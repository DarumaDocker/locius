"""Small, dependency-free Markdown -> HTML for local PDF export.

Covers what agent reports use: headings, paragraphs, bold/italic/strike, inline code, fenced code, links, images,
bullet/numbered lists (nested by indent), blockquotes, GFM tables and rules. Any raw HTML in the source is escaped,
so the output never contains scripts or tags the author didn't get from this converter.
"""
from __future__ import annotations

import html
import re
from typing import Callable

CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef\u3000-\u303f]")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _inline(text: str, img: Callable[[str], str | None] | None) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"
    text = re.sub(r"`([^`]+)`", keep, text)
    t = html.escape(text, quote=False)

    def image(m):
        alt, src = m.group(1), html.unescape(m.group(2)).strip()
        data = img(src) if img else None
        if data:
            return f'<img alt="{html.escape(alt)}" src="{data}">'
        return f'<span class="missing">[{alt or "image"}]</span>'
    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)", image, t)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2)).strip()
        if re.match(r"^(https?:|mailto:)", url, re.I):
            return f'<a href="{html.escape(url)}">{label}</a>'
        return label
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)
    t = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<strong>\2</strong>", t)
    t = re.sub(r"(?<![\*\w])\*(?=\S)(.+?)(?<=\S)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], t)


def _join(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        ln = ln.strip()
        if out and not (CJK.search(out[-1]) and CJK.search(ln[:1] or " ")):
            out += " "
        out += ln
    return out


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def md_to_html(src: str, img: Callable[[str], str | None] | None = None) -> str:
    lines = src.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    para: list[str] = []

    def flush():
        if para:
            out.append(f"<p>{_inline(_join(para), img)}</p>")
            para.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush(); i += 1; continue
        if s.startswith("```") or s.startswith("~~~"):
            flush()
            fence, body = s[:3], []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                body.append(lines[i]); i += 1
            i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(body))}</code></pre>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", s)
        if m:
            flush()
            lv = len(m.group(1))
            out.append(f"<h{lv}>{_inline(m.group(2), img)}</h{lv}>")
            i += 1; continue
        if re.match(r"^([-*_])(\s*\1){2,}$", s):
            flush(); out.append("<hr>"); i += 1; continue
        if "|" in s and i + 1 < n and TABLE_SEP.match(lines[i + 1]) and not LIST_RE.match(ln):
            flush()
            head = _cells(s)
            aligns = []
            for c in _cells(lines[i + 1]):
                aligns.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "")
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i])); i += 1
            st = lambda k: f' style="text-align:{aligns[k]}"' if k < len(aligns) and aligns[k] else ""
            t = ["<table><thead><tr>"] + [f"<th{st(k)}>{_inline(c, img)}</th>" for k, c in enumerate(head)] + ["</tr></thead><tbody>"]
            for r in rows:
                r = (r + [""] * len(head))[:max(len(head), 1)]
                t += ["<tr>"] + [f"<td{st(k)}>{_inline(c, img)}</td>" for k, c in enumerate(r)] + ["</tr>"]
            out.append("".join(t) + "</tbody></table>")
            continue
        if s.startswith(">"):
            flush()
            q = []
            while i < n and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i])); i += 1
            out.append(f"<blockquote>{md_to_html(chr(10).join(q), img)}</blockquote>")
            continue
        if LIST_RE.match(ln):
            flush()
            items: list[list] = []   # [indent, tag, start, html]
            while i < n:
                lm = LIST_RE.match(lines[i])
                if not lm:
                    # an indented line continues the previous item; anything else ends the list
                    if lines[i].strip() and lines[i].startswith("  ") and items:
                        items[-1][3] += " " + _inline(lines[i].strip(), img)
                        i += 1; continue
                    break
                marker, body = lm.group(2), lm.group(3)
                task = re.match(r"^\[([ xX])\]\s+(.*)$", body)
                if task:
                    body = ("☑ " if task.group(1).lower() == "x" else "☐ ") + task.group(2)
                items.append([len(lm.group(1)), "ol" if marker[0].isdigit() else "ul",
                              int(marker[:-1]) if marker[0].isdigit() else 1, _inline(body, img)])
                i += 1
            k, parts = 0, []
            while k < len(items):
                h_, k = _build_list(items, k)
                parts.append(h_)
            out.append("".join(parts))
            continue
        para.append(ln)
        i += 1
    flush()
    return "\n".join(out)


def _build_list(items: list[list], k: int) -> tuple[str, int]:
    ind0, tag = items[k][0], items[k][1]
    st = f' start="{items[k][2]}"' if tag == "ol" and items[k][2] != 1 else ""
    parts = [f"<{tag}{st}>"]
    while k < len(items) and items[k][0] == ind0:
        parts.append("<li>" + items[k][3])
        k += 1
        while k < len(items) and items[k][0] > ind0:   # deeper items: a nested list inside this <li>
            sub, k = _build_list(items, k)
            parts.append(sub)
        parts.append("</li>")
    parts.append(f"</{tag}>")
    return "".join(parts), k


CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Noto Sans CJK SC", "WenQuanYi Zen Hei", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
       font-size: 11pt; line-height: 1.65; color: #1f2328; }
h1 { font-size: 20pt; margin: 0 0 12pt; padding-bottom: 6pt; border-bottom: 2px solid #1f6f5c; }
h2 { font-size: 15pt; margin: 18pt 0 8pt; color: #1f6f5c; }
h3 { font-size: 12.5pt; margin: 14pt 0 6pt; }
h4, h5, h6 { font-size: 11pt; margin: 12pt 0 4pt; }
h1, h2, h3, h4 { break-after: avoid; }
p { margin: 0 0 8pt; }
ul, ol { margin: 0 0 8pt; padding-left: 20pt; }
li { margin: 2pt 0; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; font-size: 10pt; }
th, td { border: 1px solid #d0d7de; padding: 5pt 7pt; vertical-align: top; text-align: left; }
th { background: #eef4f2; font-weight: 600; }
tr { break-inside: avoid; }
code { font-family: "WenQuanYi Zen Hei Mono", "DejaVu Sans Mono", monospace; font-size: 9.5pt; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
pre { background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 6px; padding: 8pt 10pt; white-space: pre-wrap; word-break: break-word; }
pre code { background: none; padding: 0; }
blockquote { margin: 8pt 0; padding: 4pt 12pt; border-left: 3px solid #1f6f5c; color: #4b5563; background: #f7faf9; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 14pt 0; }
img { max-width: 100%; }
a { color: #1f6f5c; }
.missing { color: #9ca3af; }
"""


def page(title: str, body_html: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title or "Document")}</title>'
            f"<style>{CSS}</style></head><body>{body_html}</body></html>")
