"""Notion connector (official REST API, internal-integration token).

The user creates an internal integration at notion.so/my-integrations, copies its secret (ntn_… / secret_…)
and shares the pages/databases OMuse may use with that integration ("Connections" menu on the page).
"""
from __future__ import annotations

import os
import re
import time

import httpx

NOTION_API = os.environ.get("NOTION_API", "https://api.notion.com")
NOTION_VERSION = "2022-06-28"


class NotionError(Exception):
    pass


def norm_id(ref: str) -> str:
    """Accept a page/database id or a Notion URL; return the dashed uuid."""
    ref = str(ref or "").strip()
    if ref.startswith(("http://", "https://")) or "notion.so/" in ref or "notion.site/" in ref:
        from urllib.parse import parse_qs, urlparse
        u = urlparse(ref if "://" in ref else "https://" + ref)
        cand = (parse_qs(u.query).get("p") or [""])[0] or u.path.rstrip("/").split("/")[-1]
        m = re.search(r"([0-9a-f]{32})$", cand.replace("-", ""), re.I)
    else:
        m = re.fullmatch(r"[0-9a-f]{32}", ref.replace("-", ""), re.I)
    if not m:
        raise NotionError(f"无法识别的 Notion 页面/数据库 ID：{ref[:80]} (invalid id)")
    raw = m.group(0).lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# ------------------------------------------------------------------ rich text <-> markdown
def rt_to_md(rts: list) -> str:
    out = []
    for r in rts or []:
        t = r.get("plain_text") or (r.get("text") or {}).get("content", "")
        a = r.get("annotations") or {}
        if a.get("code"):
            t = f"`{t}`"
        if a.get("bold"):
            t = f"**{t}**"
        if a.get("italic"):
            t = f"*{t}*"
        if a.get("strikethrough"):
            t = f"~~{t}~~"
        href = r.get("href") or ((r.get("text") or {}).get("link") or {}).get("url")
        if href:
            t = f"[{t}]({href})"
        out.append(t)
    return "".join(out)


_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)\s]+\)|\*[^*]+\*)")


def md_to_rt(text: str) -> list:
    out = []
    for part in _INLINE.split(text or ""):
        if not part:
            continue
        ann, link, content = {}, None, part
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            ann["bold"], content = True, part[2:-2]
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            ann["code"], content = True, part[1:-1]
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            content, link = part[1:part.index("](")], part[part.index("](") + 2:-1]
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            ann["italic"], content = True, part[1:-1]
        for i in range(0, len(content), 1900):  # Notion limit: 2000 chars per rich-text object
            o = {"type": "text", "text": {"content": content[i:i + 1900]}}
            if link and link.startswith(("http://", "https://")):
                o["text"]["link"] = {"url": link}
            if ann:
                o["annotations"] = ann
            out.append(o)
    return out[:100]


def md_to_blocks(md: str) -> list:
    blocks, lines, i = [], (md or "").replace("\r\n", "\n").split("\n"), 0

    def b(kind, text, **extra):
        return {"object": "block", "type": kind, kind: {"rich_text": md_to_rt(text), **extra}}
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            lang = s[3:].strip() or "plain text"
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            blocks.append({"object": "block", "type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": "\n".join(body)[:1900]}}],
                "language": lang if lang in ("python", "javascript", "json", "bash", "shell", "sql", "yaml", "markdown", "html", "css", "typescript", "go", "java") else "plain text"}})
        elif not s:
            pass
        elif s in ("---", "***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif s.startswith("### "):
            blocks.append(b("heading_3", s[4:]))
        elif s.startswith("## "):
            blocks.append(b("heading_2", s[3:]))
        elif s.startswith("# "):
            blocks.append(b("heading_1", s[2:]))
        elif re.match(r"^[-*] \[( |x|X)\] ", s):
            blocks.append(b("to_do", s[6:], checked=s[3].lower() == "x"))
        elif s.startswith(("- ", "* ", "• ")):
            blocks.append(b("bulleted_list_item", s[2:]))
        elif re.match(r"^\d+[.)] ", s):
            blocks.append(b("numbered_list_item", re.sub(r"^\d+[.)] ", "", s)))
        elif s.startswith("> "):
            blocks.append(b("quote", s[2:]))
        elif s.startswith("|") and s.endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\|[\s:|-]+\|$", lines[i].strip()):
                    rows.append(lines[i].strip())
                i += 1
            i -= 1
            blocks.append({"object": "block", "type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": "\n".join(rows)[:1900]}}], "language": "plain text"}})
        else:
            para = [s]
            while i + 1 < len(lines) and lines[i + 1].strip() and not re.match(r"^(#{1,3} |[-*•] |\d+[.)] |> |```|\||---)", lines[i + 1].strip()):
                i += 1
                para.append(lines[i].strip())
            blocks.append(b("paragraph", " ".join(para)))
        i += 1
    return blocks[:100]


def blocks_to_md(blocks: list, depth: int = 0) -> list[str]:
    out, ind = [], "  " * depth
    for bl in blocks:
        t = bl.get("type", "")
        d = bl.get(t) or {}
        txt = rt_to_md(d.get("rich_text") or [])
        line = {
            "paragraph": txt, "heading_1": f"# {txt}", "heading_2": f"## {txt}", "heading_3": f"### {txt}",
            "bulleted_list_item": f"- {txt}", "numbered_list_item": f"1. {txt}", "quote": f"> {txt}",
            "to_do": f"- [{'x' if d.get('checked') else ' '}] {txt}", "toggle": f"▸ {txt}", "callout": f"> 💡 {txt}",
            "code": f"```\n{txt}\n```", "divider": "---", "child_page": f"📄 子页面 child page: {d.get('title', '')} (id {bl.get('id')})",
            "child_database": f"🗂 子数据库 child database: {d.get('title', '')} (id {bl.get('id')})",
            "bookmark": f"🔖 {d.get('url', '')}", "equation": f"$${(d.get('expression') or '')}$$",
            "image": "[图片 image]", "file": "[文件 file]", "pdf": "[PDF]", "table": "[表格 table]", "table_row": " | ".join(rt_to_md(c) for c in d.get("cells") or []),
        }.get(t, f"[{t}]")
        out.append(ind + line)
        if bl.get("_children"):
            out += blocks_to_md(bl["_children"], depth + 1)
    return out


def prop_to_text(p: dict) -> str:
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return rt_to_md(v)
    if t in ("select", "status"):
        return (v or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in v or [])
    if t == "date":
        return ((v or {}).get("start") or "") + (f" → {v['end']}" if v and v.get("end") else "")
    if t == "people":
        return ", ".join(x.get("name", x.get("id", "")) for x in v or [])
    if t in ("number", "checkbox", "url", "email", "phone_number", "created_time", "last_edited_time"):
        return "" if v is None else str(v)
    if t == "formula":
        return str((v or {}).get((v or {}).get("type"), ""))
    if t == "relation":
        return ", ".join(x.get("id", "") for x in v or [])
    return ""


def page_title(page: dict) -> str:
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return rt_to_md(p.get("title")) or "(无标题 untitled)"
    if page.get("object") == "database":
        return rt_to_md(page.get("title")) or "(无标题 untitled)"
    return "(无标题 untitled)"


def page_brief(page: dict) -> dict:
    props = {k: prop_to_text(v) for k, v in (page.get("properties") or {}).items() if v.get("type") != "title"}
    return {"id": page.get("id"), "object": page.get("object"), "title": page_title(page), "url": page.get("url", ""),
            "last_edited_time": page.get("last_edited_time", ""), "archived": page.get("archived", False),
            "properties": {k: v for k, v in props.items() if v}}


class Notion:
    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self.c = httpx.Client(base_url=NOTION_API, timeout=timeout, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})

    def close(self):
        self.c.close()

    def _req(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        for attempt in range(3):
            try:
                r = self.c.request(method, path, json=json, params=params)
            except httpx.HTTPError as e:
                raise NotionError(f"无法连接 Notion (network): {type(e).__name__}")
            if r.status_code == 429 and attempt < 2:
                time.sleep(min(float(r.headers.get("retry-after", "1") or 1), 5))
                continue
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.status_code == 401:
                raise NotionError("Notion 令牌无效或已被撤销 (invalid token)")
            if r.status_code == 404:
                raise NotionError("找不到这个页面/数据库，或者还没有把它共享给 OMuse 集成 "
                                  "(not found — share the page with your integration via ••• → Connections)")
            if r.status_code >= 400:
                raise NotionError(f"Notion 错误 {r.status_code}: {str(data.get('message', ''))[:300]}")
            return data
        raise NotionError("Notion 请求过于频繁 (rate limited)")

    # ---------------------------------------------------------------- read
    def me(self) -> dict:
        return self._req("GET", "/v1/users/me")

    def search(self, query: str = "", kind: str = "", limit: int = 10) -> list[dict]:
        body: dict = {"page_size": max(1, min(int(limit or 10), 50))}
        if query:
            body["query"] = query
        if kind in ("page", "database"):
            body["filter"] = {"property": "object", "value": kind}
        body["sort"] = {"direction": "descending", "timestamp": "last_edited_time"}
        return [page_brief(p) for p in self._req("POST", "/v1/search", body).get("results", [])]

    def children(self, block_id: str, depth: int = 0, budget: list | None = None) -> list[dict]:
        budget = budget if budget is not None else [300]
        out, cursor = [], None
        while budget[0] > 0:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self._req("GET", f"/v1/blocks/{block_id}/children", params=params)
            for bl in data.get("results", []):
                budget[0] -= 1
                if bl.get("has_children") and depth < 1 and bl.get("type") not in ("child_page", "child_database") and budget[0] > 0:
                    bl["_children"] = self.children(bl["id"], depth + 1, budget)
                out.append(bl)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    def get_page(self, ref: str) -> dict:
        pid = norm_id(ref)
        page = self._req("GET", f"/v1/pages/{pid}")
        md = "\n".join(blocks_to_md(self.children(pid)))
        return {**page_brief(page), "content": md[:40000]}

    def database(self, ref: str) -> dict:
        return self._req("GET", f"/v1/databases/{norm_id(ref)}")

    def query_database(self, ref: str, filter: dict | None = None, sorts: list | None = None, limit: int = 20) -> dict:
        db = self.database(ref)
        body: dict = {"page_size": max(1, min(int(limit or 20), 100))}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        res = self._req("POST", f"/v1/databases/{db['id']}/query", body)
        schema = {k: v.get("type") for k, v in (db.get("properties") or {}).items()}
        return {"database": page_title(db), "database_id": db["id"], "schema": schema,
                "rows": [page_brief(p) for p in res.get("results", [])], "has_more": res.get("has_more", False)}

    # ---------------------------------------------------------------- write
    def _props_for(self, db: dict, props: dict) -> dict:
        out = {}
        schema = db.get("properties") or {}
        for k, v in (props or {}).items():
            if k not in schema:
                raise NotionError(f"数据库里没有「{k}」这一列 (unknown property). 可用 columns: {', '.join(schema)}")
            t = schema[k]["type"]
            if t == "title":
                out[k] = {"title": md_to_rt(str(v))}
            elif t == "rich_text":
                out[k] = {"rich_text": md_to_rt(str(v))}
            elif t in ("select", "status"):
                out[k] = {t: {"name": str(v)} if v not in (None, "") else None}
            elif t == "multi_select":
                vals = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
                out[k] = {"multi_select": [{"name": str(x)} for x in vals]}
            elif t == "date":
                if isinstance(v, dict):
                    out[k] = {"date": v}
                else:
                    out[k] = {"date": {"start": str(v)} if v else None}
            elif t == "checkbox":
                out[k] = {"checkbox": v if isinstance(v, bool) else str(v).lower() in ("true", "yes", "1", "是", "✓")}
            elif t == "number":
                out[k] = {"number": float(v) if v not in (None, "") else None}
            elif t in ("url", "email", "phone_number"):
                out[k] = {t: str(v) or None}
            else:
                raise NotionError(f"暂不支持写入「{k}」({t}) 类型的列 (unsupported property type)")
        return out

    def create_page(self, parent: str, title: str, content: str = "", properties: dict | None = None) -> dict:
        pid = norm_id(parent)
        blocks = md_to_blocks(content)
        # parent may be a database or a page
        try:
            db = self._req("GET", f"/v1/databases/{pid}")
        except NotionError:
            db = None
        if db:
            props = self._props_for(db, properties or {})
            tkey = next(k for k, v in db["properties"].items() if v["type"] == "title")
            props.setdefault(tkey, {"title": md_to_rt(title)})
            body = {"parent": {"database_id": pid}, "properties": props}
        else:
            if properties:
                raise NotionError("父级是普通页面时不能设置属性列 (properties only for database rows)")
            body = {"parent": {"page_id": pid}, "properties": {"title": {"title": md_to_rt(title)}}}
        if blocks:
            body["children"] = blocks
        page = self._req("POST", "/v1/pages", body)
        return {"created": True, **page_brief(page)}

    def append(self, ref: str, content: str) -> dict:
        pid = norm_id(ref)
        blocks = md_to_blocks(content)
        if not blocks:
            raise NotionError("没有要追加的内容 (empty content)")
        self._req("PATCH", f"/v1/blocks/{pid}/children", {"children": blocks})
        return {"appended_blocks": len(blocks), "page_id": pid}

    def update_page(self, ref: str, properties: dict | None = None, archived: bool | None = None, title: str | None = None) -> dict:
        pid = norm_id(ref)
        page = self._req("GET", f"/v1/pages/{pid}")
        body: dict = {}
        parent = page.get("parent") or {}
        if properties:
            if parent.get("type") != "database_id":
                raise NotionError("只有数据库里的行才能改属性列 (properties only for database rows)")
            body["properties"] = self._props_for(self.database(parent["database_id"]), properties)
        if title:
            tkey = next((k for k, v in (page.get("properties") or {}).items() if v.get("type") == "title"), "title")
            body.setdefault("properties", {})[tkey] = {"title": md_to_rt(title)}
        if archived is not None:
            body["archived"] = bool(archived)
        if not body:
            raise NotionError("没有要修改的内容 (nothing to update)")
        return {"updated": True, **page_brief(self._req("PATCH", f"/v1/pages/{pid}", body))}

    # ---------------------------------------------------------------- watch
    def edited_since(self, ref: str, since: str) -> tuple[str, list[dict]]:
        """Rows edited on/after `since` (Notion timestamps are rounded to the minute, so callers dedupe)."""
        db = self.database(ref)
        body = {"page_size": 50, "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
                "filter": {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}}}
        return page_title(db), self._req("POST", f"/v1/databases/{db['id']}/query", body).get("results", [])
