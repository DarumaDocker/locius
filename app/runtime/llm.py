"""OpenAI-compatible chat client (Olares Router / llama.cpp / any endpoint). Replaceable by design."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import httpx

_THINK = re.compile(r"<think>.*?</think>", re.S)
_TOOLCALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


class LLMError(Exception):
    pass


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
        self.sem = asyncio.Semaphore(1)  # one local GPU → serialize calls
        self.fallback: dict[str, str] = {}  # configured model -> model actually served (when the configured one is missing)

    @staticmethod
    def _model_missing(r) -> bool:
        t = (r.text or "").lower()
        return r.status_code == 404 or ("model" in t and any(k in t for k in ("not found", "does not exist", "unknown", "not exist", "no such")))

    async def first_model(self, base: str, exclude: str = "") -> str:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{base}/models")
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:
            return ""
        ids = [i for i in ids if i != exclude and not any(k in i.lower() for k in ("embed", "rerank", "whisper", "tts"))]
        return ids[0] if ids else ""

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, *, temperature: float | None = None,
                   max_tokens: int | None = None, purpose: str = "executor", task_id: str = "", model: str | None = None,
                   no_think: bool = False) -> dict:
        s = self.get_settings()
        base = str(s["model_base_url"]).rstrip("/")
        want = model or s["model_name"]
        body = {
            "model": self.fallback.get(want, want),
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
        async with self.sem:
            for attempt in range(4):
                t0 = time.time()
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as c:
                        r = await c.post(f"{base}/chat/completions", json=body)
                    if r.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    if r.status_code >= 400:
                        # some servers reject chat_template_kwargs; retry once without it
                        if self._model_missing(r) and want not in self.fallback:
                            alt = await self.first_model(base, body["model"])
                            if alt:
                                self.fallback[want] = alt
                                body["model"] = alt
                                continue
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
        usage = data.get("usage") or {}
        if self.on_call:
            try:
                await self.on_call({"purpose": purpose, "task_id": task_id, "model": body["model"],
                                    "latency_s": round(latency, 2), "prompt_tokens": usage.get("prompt_tokens"),
                                    "completion_tokens": usage.get("completion_tokens"), "tool_calls": [c["name"] for c in calls]})
            except Exception:
                pass
        return {"content": content, "reasoning": reasoning, "tool_calls": calls, "usage": usage,
                "finish_reason": data["choices"][0].get("finish_reason")}
