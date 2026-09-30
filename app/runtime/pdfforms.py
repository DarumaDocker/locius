"""Fill PDF forms (AcroForm) locally — permission slips, applications, school forms.

The agent lists the fields, asks the user for what it doesn't know, fills a *copy*, and shows it to the user before
anything is sent. Nothing leaves the machine. Signature fields are never filled (the user signs).
"""
from __future__ import annotations

import difflib
import os

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

YES = {"yes", "y", "true", "1", "on", "x", "✓", "✔", "checked", "是", "对", "勾选", "同意"}
NO = {"no", "n", "false", "0", "off", "", "unchecked", "否", "不", "不同意"}
TYPES = {"/Tx": "text", "/Btn": "button", "/Ch": "choice", "/Sig": "signature"}


class FormError(Exception):
    pass


def _states(f) -> list[str]:
    st = f.get("/_States_") or []
    return [str(s) for s in st if str(s) != "/Off"]


def _kind(f) -> str:
    ft = TYPES.get(str(f.get("/FT", "")), "unknown")
    if ft == "button":
        flags = int(f.get("/Ff", 0) or 0)
        if flags & (1 << 16):
            return "pushbutton"
        return "radio" if flags & (1 << 15) else "checkbox"
    if ft == "choice":
        return "dropdown" if int(f.get("/Ff", 0) or 0) & (1 << 17) else "list"
    return ft


def _options(f) -> list[str]:
    out = []
    for o in f.get("/Opt") or []:
        o = o.get_object() if hasattr(o, "get_object") else o
        out.append(str(o[0]) if isinstance(o, list) and o else str(o))   # [export value, display text] or plain text
    return out


def _pages(reader: PdfReader) -> dict[str, int]:
    where = {}
    for i, page in enumerate(reader.pages, 1):
        for a in page.get("/Annots") or []:
            a = a.get_object()
            node, parts = a, []
            while node is not None:
                if node.get("/T") is not None:
                    parts.append(str(node["/T"]))
                parent = node.get("/Parent")
                node = parent.get_object() if parent is not None else None
            if parts:
                where.setdefault(".".join(reversed(parts)), i)
    return where


def fields(path: str) -> list[dict]:
    try:
        reader = PdfReader(path)
    except Exception as e:
        raise FormError(f"不是有效的 PDF (not a readable PDF): {e}")
    if reader.is_encrypted:
        raise FormError("PDF 有密码保护 (encrypted PDF)")
    got = reader.get_fields() or {}
    if not got:
        raise FormError("这个 PDF 没有可填写的表单字段（可能是扫描件或普通 PDF）。This PDF has no fillable form fields "
                        "(it may be a scan); fill it by writing a new document instead.")
    pages = _pages(reader)
    out = []
    for name, f in got.items():
        kind = _kind(f)
        if kind == "pushbutton":
            continue
        item = {"name": name, "type": kind, "value": str(f.get("/V", "") or ""), "page": pages.get(name)}
        if f.get("/TU"):
            item["label"] = str(f["/TU"])
        if kind in ("checkbox", "radio"):
            item["options"] = [s.lstrip("/") for s in _states(f)]
            item["value"] = str(f.get("/V", "") or "").lstrip("/")
        elif kind in ("dropdown", "list"):
            item["options"] = _options(f)
        if int(f.get("/Ff", 0) or 0) & 2:
            item["required"] = True
        if int(f.get("/Ff", 0) or 0) & 1:
            item["read_only"] = True
        out.append(item)
    return out


def fill(path: str, values: dict, output: str) -> dict:
    """Write a filled copy. Returns what was set, what was skipped and why."""
    info = {f["name"]: f for f in fields(path)}
    reader = PdfReader(path)
    raw = reader.get_fields() or {}
    writer = PdfWriter(clone_from=reader)
    ok, problems, text_vals, btn_vals = {}, [], {}, {}
    for name, val in (values or {}).items():
        name = str(name)
        f = info.get(name)
        if not f:
            near = difflib.get_close_matches(name, list(info), n=3, cutoff=0.5)
            problems.append(f"unknown field '{name}'" + (f" (did you mean {', '.join(near)}?)" if near else ""))
            continue
        if f["type"] == "signature":
            problems.append(f"'{name}' is a signature field — the user must sign it themselves")
            continue
        if f.get("read_only"):
            problems.append(f"'{name}' is read-only")
            continue
        v = "" if val is None else str(val).strip()
        if f["type"] == "checkbox":
            on = (f.get("options") or ["Yes"])[0]
            if isinstance(val, bool):
                state = on if val else "Off"
            elif v.lower() in YES or v.lstrip("/") == on:
                state = on
            elif v.lower() in NO or v.lstrip("/") == "Off":
                state = "Off"
            else:
                problems.append(f"'{name}' is a checkbox: use true/false")
                continue
            btn_vals[name] = state
            ok[name] = state != "Off"
        elif f["type"] == "radio":
            opts = f.get("options") or []
            m = next((o for o in opts if o.lower() == v.lstrip("/").lower()), None)
            if not m:
                problems.append(f"'{name}' must be one of {opts}")
                continue
            btn_vals[name] = m
            ok[name] = m
        elif f["type"] in ("dropdown", "list"):
            opts = f.get("options") or []
            if opts and v not in opts:
                m = next((o for o in opts if o.lower() == v.lower()), None)
                if not m:
                    problems.append(f"'{name}' must be one of {opts}")
                    continue
                v = m
            text_vals[name] = v
            ok[name] = v
        else:
            text_vals[name] = v
            ok[name] = v
    if not ok:
        raise FormError("没有填写任何字段 (nothing was filled): " + "; ".join(problems))
    for page in writer.pages:
        if text_vals:
            writer.update_page_form_field_values(page, text_vals, auto_regenerate=False)
        for a in page.get("/Annots") or []:
            w = a.get_object()
            parent = w.get("/Parent")
            fname = str(w.get("/T") if w.get("/T") is not None else (parent.get_object().get("/T") if parent is not None else ""))
            if fname not in btn_vals:
                continue
            state = btn_vals[fname]
            target = parent.get_object() if parent is not None and w.get("/T") is None else w
            target[NameObject("/V")] = NameObject("/" + state)
            aps = list((w.get("/AP") or {}).get("/N", {}).keys()) if w.get("/AP") else []
            w[NameObject("/AS")] = NameObject("/" + state if ("/" + state) in aps else "/Off")
    writer.set_need_appearances_writer(True)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    with open(output, "wb") as fh:
        writer.write(fh)
    # read back: what the filled copy actually contains
    after = {f["name"]: f["value"] for f in fields(output)}
    mismatched = [n for n, v in ok.items() if isinstance(v, str) and after.get(n, "").lstrip("/") != v]
    if mismatched:
        problems.append("could not verify: " + ", ".join(mismatched))
    empty_required = [n for n, f in info.items() if f.get("required") and not after.get(n) and n not in ok]
    return {"output": output, "filled": ok, "problems": problems, "required_still_empty": empty_required,
            "untouched": [n for n in info if n not in ok and not after.get(n)][:40]}
