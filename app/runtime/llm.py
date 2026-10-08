"""OpenAI-compatible chat client (Olares Router / llama.cpp / any endpoint). Replaceable by design."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

import httpx

_THINK = re.compile(r"<think>.*?</think>", re.S)
_TOOLCALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
# Qwen3 "XML" style that llama.cpp sometimes leaves in the text: <tool_call><function=x><parameter=k>v</parameter></function></tool_call>
_TOOLCALL_XML = re.compile(r"<tool_call>\s*<function=([\w.\-]+)>(.*?)</function>\s*(?:</tool_call>|$)", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", re.S)
_ANY_TOOLCALL = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.S)


def _xml_value(v: str):
    try:
        return json.loads(v)
    except Exception:
        return v


def strip_tool_markup(text: str) -> str:
    """Remove tool-call markup a model wrote as plain text (it must never reach the user as the answer)."""
    return _ANY_TOOLCALL.sub("", text or "").strip()


def auth_headers() -> dict:
    """Bearer key for hosted OpenAI-compatible APIs (OMUSE_MODEL_API_KEY). Read from the environment only, so it never
    shows up in Settings or the audit log; the Olares router needs none."""
    key = (os.environ.get("OMUSE_MODEL_API_KEY") or os.environ.get("PERSONA_MODEL_API_KEY") or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def pick_chat_model(models: list[dict], exclude: str = "") -> str:
    """A model that can chat: the router's mode says so when it reports one (tts / audio / embedding models are never picked),
    otherwise judge by the name. Models that are still loading are skipped."""
    out = []
    for m in models:
        mid = str(m.get("id") or "")
        if not mid or mid == exclude:
            continue
        mode = str(m.get("mode") or "").lower()
        if mode and mode != "chat":
            continue
        if str(m.get("readiness") or "ready").lower() not in ("ready", "running", "loaded"):
            continue
        if any(k in mid.lower() for k in ("embed", "rerank", "whisper", "tts", "kokoro", "speech", "audio")):
            continue
        out.append(mid)
    return out[0] if out else ""


class LLMError(Exception):
    pass


class LLMContextError(LLMError):
    """The prompt is larger than the model server's context window: retrying the same request cannot help."""
    def __init__(self, msg: str, chars: int = 0):
        super().__init__(msg)
        self.chars = chars


def _context_exceeded(text: str) -> bool:
    t = (text or "").lower()
    return ("context" in t and any(k in t for k in ("exceed", "too long", "too large", "maximum context", "context length",
                                                     "context size", "n_ctx"))) or "prompt is too long" in t


def parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    s = str(raw).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    s2 = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(s2)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"_unparsed": s[:500]}


def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    if not text:
        return None
    text = _THINK.sub("", text)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    cand = m.group(1) if m else text
    for opener, closer in (("{", "}"), ("[", "]")):
        i = cand.find(opener)
        j = cand.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(cand[i:j + 1])
            except Exception:
                try:
                    return json.loads(re.sub(r",\s*([}\]])", r"\1", cand[i:j + 1]))
                except Exception:
                    continue
    return None


class LLM:
    def __init__(self, get_settings, on_call=None):
        self.get_settings = get_settings
        self.on_call = on_call
        # Requests in flight at once. llama.cpp serves 2 slots (-np 2) and batches them, so two tasks no longer wait for
        # each other's calls (with 1, a one-step translation waited ~50 s behind another task's long prefill).
        try:
            n = int((get_settings() or {}).get("llm_concurrency") or 2)
        except Exception:
            n = 2
        self.sem = asyncio.Semaphore(max(1, min(n, 8)))
        self.stt_sem = asyncio.Semaphore(1)   # speech-to-text runs on its own server; it must not hold an LLM slot
        # configured model -> (model actually served, until when). Only for a few minutes: while the Olares router restarts,
        # the configured model is briefly "missing"; a permanent fallback kept every task on whatever model was listed
        # first — on 2026-10-05 that was a text-to-speech model, and every chat failed with HTTP 422 for a day.
        self.fallback: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _model_missing(r) -> bool:
        t = (r.text or "").lower()
        return r.status_code == 404 or ("model" in t and any(k in t for k in ("not found", "does not exist", "unknown", "not exist", "no such")))

    async def first_model(self, base: str, exclude: str = "") -> str:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{base}/models", headers=auth_headers())
            models = [m for m in (r.json().get("data") or []) if isinstance(m, dict) and m.get("id")]
        except Exception:
            return ""
        return pick_chat_model(models, exclude)

    async def stt_model(self) -> str:
        """Speech-to-text model: Settings → stt_model, else the first whisper-like model the endpoint serves ('' = none)."""
        s = self.get_settings()
        if s.get("stt_model"):
            return str(s["stt_model"])
        base = str(s["model_base_url"]).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{base}/models", headers=auth_headers())
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:
            return ""
        for i in ids:
            if any(k in i.lower() for k in ("whisper", "sensevoice", "paraformer", "asr", "speech-to-text", "stt")):
                return i
        return ""

    async def transcribe(self, audio: bytes, filename: str = "audio.wav", task_id: str = "") -> str:
        """OpenAI-compatible /audio/transcriptions. Raises LLMError when no speech model is available."""
        s = self.get_settings()
        base = str(s["model_base_url"]).rstrip("/")
        model = await self.stt_model()
        if not model:
            raise LLMError("没有语音转文字模型 (no speech-to-text model on the model endpoint; set Settings → Speech-to-text model)")
        t0 = time.time()
        async with self.stt_sem:
            async with httpx.AsyncClient(timeout=httpx.Timeout(float(s.get("llm_timeout") or 600), connect=15)) as c:
                r = await c.post(f"{base}/audio/transcriptions", data={"model": model, "response_format": "json"},
                                 files={"file": (filename, audio, "audio/wav")}, headers=auth_headers())
        if r.status_code >= 400:
            raise LLMError(f"语音转文字失败 transcription failed HTTP {r.status_code}: {r.text[:200]}")
        try:
            text = r.json().get("text") or ""
        except Exception:
            text = r.text
        if self.on_call:
            try:
                await self.on_call({"purpose": "stt", "task_id": task_id, "model": model, "latency_s": round(time.time() - t0, 2),
                                    "prompt_tokens": None, "completion_tokens": None, "tool_calls": []})
            except Exception:
                pass
        return text.strip()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *, temperature: float | None = None,
                   max_tokens: int | None = None, purpose: str = "executor", task_id: str = "", model: str | None = None,
                   no_think: bool = False) -> dict:
        s = self.get_settings()
        base = str(s["model_base_url"]).rstrip("/")
        want = model or s["model_name"]
        fb = self.fallback.get(want)
        if fb and fb[1] < time.time():
            self.fallback.pop(want, None)
            fb = None
        body = {
            "model": fb[0] if fb else want,
            "messages": messages,
            "temperature": float(s["temperature"] if temperature is None else temperature),
            "max_tokens": int(max_tokens or s["max_tokens"]),
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if s.get("disable_thinking") or no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        extra = s.get("extra_body")
        if extra:
            try:
                body.update(json.loads(extra) if isinstance(extra, str) else extra)
            except Exception:
                pass
        timeout = float(s.get("llm_timeout") or 600)
        last_err = None
        queued = time.time()
        async with self.sem:
            wait_s = round(time.time() - queued, 2)
            for attempt in range(4):
                t0 = time.time()
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as c:
                        r = await c.post(f"{base}/chat/completions", json=body, headers=auth_headers())
                    if r.status_code in (400, 413, 500) and _context_exceeded(r.text):
                        raise LLMContextError(f"上下文太长 (prompt exceeds the model's context window): {r.text[:200]}",
                                              chars=sum(len(str(m.get("content") or "")) for m in messages))
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    if r.status_code >= 400:
                        # some servers reject chat_template_kwargs; retry once without it
                        if body["model"] != want and not _context_exceeded(r.text):
                            # the stand-in model fails: forget it and try the configured model again
                            self.fallback.pop(want, None)
                            body["model"] = want
                            continue
                        if self._model_missing(r) and want not in self.fallback:
                            alt = await self.first_model(base, body["model"])
                            if alt:
                                self.fallback[want] = (alt, time.time() + 300)
                                body["model"] = alt
                                continue
                            raise LLMError(f"设置里的模型 {want} 现在不可用（可能正在加载或已被卸载），也没有别的聊天模型可用 "
                                           f"(the configured model is not available right now): HTTP {r.status_code}: {r.text[:200]}")
                        if "chat_template_kwargs" in body and attempt == 0:
                            body.pop("chat_template_kwargs", None)
                            continue
                        raise LLMError(f"模型接口错误 model API error HTTP {r.status_code}: {r.text[:300]}")
                    data = r.json()
                    break
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = f"{type(e).__name__}: {e}"
                    await asyncio.sleep(3 * (attempt + 1))
            else:
                raise LLMError(f"模型服务暂时不可用 (model unavailable after retries): {last_err}")
        latency = time.time() - t0
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            raise LLMError(f"模型返回格式异常 unexpected response: {str(data)[:300]}")
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or ""
        m = re.search(r"<think>(.*?)</think>", content, re.S)
        if m:
            reasoning = reasoning or m.group(1)
        content = _THINK.sub("", content)
        if "<think>" in content and "</think>" not in content:  # truncated thinking
            content = content.split("<think>")[0]
        content = content.strip()
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if not fn.get("name"):
                continue
            calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:10]}", "name": fn["name"],
                          "args": parse_args(fn.get("arguments"))})
        if not calls and tools and "<tool_call>" in content:
            for raw in _TOOLCALL_TAG.findall(content):
                j = parse_args(raw)
                if j.get("name"):
                    calls.append({"id": f"call_{uuid.uuid4().hex[:10]}", "name": j["name"],
                                  "args": parse_args(j.get("arguments") or j.get("parameters") or {})})
            content = _TOOLCALL_TAG.sub("", content).strip()
        if not calls and tools and "<function=" in content:
            for name, body in _TOOLCALL_XML.findall(content):
                calls.append({"id": f"call_{uuid.uuid4().hex[:10]}", "name": name,
                              "args": {k: _xml_value(v) for k, v in _XML_PARAM.findall(body)}})
        content = strip_tool_markup(content)   # also when no tools were offered (final answer): never show raw markup
        usage = data.get("usage") or {}
        if self.on_call:
            try:
                await self.on_call({"purpose": purpose, "task_id": task_id, "model": body["model"],
                                    "latency_s": round(latency, 2), "wait_s": wait_s, "prompt_tokens": usage.get("prompt_tokens"),
                                    "completion_tokens": usage.get("completion_tokens"), "tool_calls": [c["name"] for c in calls]})
            except Exception:
                pass
        return {"content": content, "reasoning": reasoning, "tool_calls": calls, "usage": usage,
                "finish_reason": data["choices"][0].get("finish_reason")}
