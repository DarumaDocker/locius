"""Charts and diagrams as SVG, in pure Python (no plotting library). The browser container turns the SVG into a PNG
(JavaScript off, no network), so Chinese text uses the system CJK fonts.

Types: line (trends, several series, highest/lowest marked), bar (grouped; horizontal for rankings), pie (shares),
gantt (schedules) and flow (flowcharts / architecture: boxes and arrows)."""
from __future__ import annotations

import datetime as _dt
import html
import math
import re

W, H = 960, 540
FONT = "'Noto Sans CJK SC','Noto Sans SC','Source Han Sans SC','PingFang SC','Microsoft YaHei','WenQuanYi Micro Hei',sans-serif"
PALETTE = ["#2563eb", "#f97316", "#16a34a", "#dc2626", "#9333ea", "#0891b2", "#ca8a04", "#db2777", "#4b5563", "#65a30d"]
INK, MUTED, GRID, BG = "#111827", "#6b7280", "#e5e7eb", "#ffffff"
TYPES = ("line", "bar", "pie", "gantt", "flow")


class ChartError(ValueError):
    pass


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _tw(s: str, size: float) -> float:
    """Rough text width: CJK glyphs are ~1 em wide, Latin ~0.58 em."""
    return sum(size * (1.0 if ord(ch) > 0x2e80 else 0.58) for ch in str(s))


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("$", "").replace("¥", "").replace("HK", "")
    try:
        return float(s)
    except ValueError:
        raise ChartError(f"不是数字 not a number: {v!r}")


def _fmt(v: float, unit: str = "") -> str:
    a = abs(v)
    if a >= 1e12:
        s = f"{v / 1e12:.2f}T"
    elif a >= 1e9:
        s = f"{v / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"{v / 1e6:.2f}M"
    elif a >= 1e4:
        s = f"{v:,.0f}"
    elif a >= 100:
        s = f"{v:,.1f}".rstrip("0").rstrip(".")
    else:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s + unit


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi == lo:
        hi, lo = hi + (abs(hi) or 1) * 0.5, lo - (abs(lo) or 1) * 0.5
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _frame(title: str, subtitle: str, source: str, body: str, w: int = W, h: int = H) -> str:
    head = f'<text x="32" y="40" font-size="22" font-weight="700" fill="{INK}">{_e(title)}</text>' if title else ""
    if subtitle:
        head += f'<text x="32" y="64" font-size="14" fill="{MUTED}">{_e(subtitle)}</text>'
    foot = (f'<text x="32" y="{h - 14}" font-size="12" fill="{MUTED}">{_e("来源 Source: " + source)}</text>'
            if source else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="{FONT}"><rect width="{w}" height="{h}" fill="{BG}"/>{head}{body}{foot}</svg>')


def _legend(names: list[str], x: float, y: float, maxw: float) -> str:
    out, cx, cy = [], x, y
    for i, n in enumerate(names):
        wd = 22 + _tw(n, 13) + 18
        if cx + wd > x + maxw and cx > x:
            cx, cy = x, cy + 20
        c = PALETTE[i % len(PALETTE)]
        out.append(f'<rect x="{cx}" y="{cy - 10}" width="14" height="10" rx="2" fill="{c}"/>'
                   f'<text x="{cx + 20}" y="{cy}" font-size="13" fill="{INK}">{_e(n)}</text>')
        cx += wd
    return "".join(out)


def _series(spec: dict, n: int) -> list[dict]:
    ser = spec.get("series")
    if not ser and spec.get("values") is not None:
        ser = [{"name": spec.get("name") or "", "values": spec["values"]}]
    if not ser:
        raise ChartError("需要 series（[{name, values}]）或 values")
    out = []
    for s in ser[:10]:
        vals = [_num(v) for v in (s.get("values") or [])]
        if len(vals) != n:
            raise ChartError(f"series「{s.get('name', '')}」有 {len(vals)} 个数值，但 labels 有 {n} 个 (values must match labels)")
        out.append({"name": str(s.get("name") or ""), "values": vals})
    return out


def _plot_area(has_legend: bool, has_sub: bool):
    top = 92 if has_sub else 76
    top += 22 if has_legend else 0
    return 78, top, W - 40, H - 70   # left, top, right, bottom


# ----------------------------------------------------------------------------------------------- line
def line(spec: dict) -> str:
    labels = [str(x) for x in (spec.get("labels") or spec.get("x") or [])]
    if len(labels) < 2:
        raise ChartError("折线图至少需要 2 个点 (line chart needs labels with at least 2 points)")
    ser = _series(spec, len(labels))
    unit = str(spec.get("unit") or "")
    allv = [v for s in ser for v in s["values"] if v is not None]
    if not allv:
        raise ChartError("没有数值 (no values)")
    multi = len(ser) > 1
    L, T, R, B = _plot_area(multi, bool(spec.get("subtitle")))
    pad = (max(allv) - min(allv)) * 0.08 or abs(max(allv)) * 0.05 or 1
    ticks = _nice_ticks(min(allv) - pad, max(allv) + pad)
    lo, hi = ticks[0], ticks[-1]
    xs = lambda i: L + (R - L) * i / (len(labels) - 1)
    ys = lambda v: B - (B - T) * (v - lo) / (hi - lo)
    g = []
    for t in ticks:
        y = ys(t)
        g.append(f'<line x1="{L}" y1="{y:.1f}" x2="{R}" y2="{y:.1f}" stroke="{GRID}"/>'
                 f'<text x="{L - 8}" y="{y + 4:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{_e(_fmt(t, unit))}</text>')
    step = max(1, math.ceil(len(labels) / 10))
    for i in range(0, len(labels), step):
        g.append(f'<text x="{xs(i):.1f}" y="{B + 20}" font-size="12" fill="{MUTED}" text-anchor="middle">{_e(labels[i])}</text>')
    if (len(labels) - 1) % step:
        g.append(f'<text x="{xs(len(labels) - 1):.1f}" y="{B + 20}" font-size="12" fill="{MUTED}" text-anchor="middle">'
                 f'{_e(labels[-1])}</text>')
    g.append(f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" stroke="#9ca3af"/>')
    for k, s in enumerate(ser):
        c = PALETTE[k % len(PALETTE)]
        pts, segs = [], []
        for i, v in enumerate(s["values"]):
            if v is None:
                if pts:
                    segs.append(pts)
                pts = []
            else:
                pts.append(f"{xs(i):.1f},{ys(v):.1f}")
        if pts:
            segs.append(pts)
        for p in segs:
            g.append(f'<polyline points="{" ".join(p)}" fill="none" stroke="{c}" stroke-width="2.5" stroke-linejoin="round"/>')
        if len(labels) <= 24:
            for i, v in enumerate(s["values"]):
                if v is not None:
                    g.append(f'<circle cx="{xs(i):.1f}" cy="{ys(v):.1f}" r="3" fill="{c}"/>')
    if spec.get("mark_extremes", len(ser) == 1):
        for k, s in enumerate(ser[:2]):
            vals = [(v, i) for i, v in enumerate(s["values"]) if v is not None]
            for (v, i), word, dy in ((max(vals), "最高 High", -12), (min(vals), "最低 Low", 22)):
                c = PALETTE[k % len(PALETTE)]
                x, y = xs(i), ys(v)
                if y + dy > B - 6 or y + dy < T + 4:   # keep the note off the axis / title
                    dy = -dy if dy > 0 else 22
                txt = f"{word} {_fmt(v, unit)} ({labels[i]})"
                anchor = "end" if x > R - _tw(txt, 12) / 2 else ("start" if x < L + _tw(txt, 12) / 2 else "middle")
                g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="none" stroke="{c}" stroke-width="2"/>'
                         f'<text x="{x:.1f}" y="{y + dy:.1f}" font-size="12" font-weight="700" fill="{c}" '
                         f'text-anchor="{anchor}">{_e(txt)}</text>')
    if spec.get("y_label"):
        g.append(f'<text x="18" y="{(T + B) / 2}" font-size="12" fill="{MUTED}" transform="rotate(-90 18 {(T + B) / 2})" '
                 f'text-anchor="middle">{_e(spec["y_label"])}</text>')
    if multi:
        g.append(_legend([s["name"] for s in ser], L, T - 14, R - L))
    return _frame(spec.get("title", ""), spec.get("subtitle", ""), spec.get("source", ""), "".join(g))


# ----------------------------------------------------------------------------------------------- bar
def bar(spec: dict) -> str:
    labels = [str(x) for x in (spec.get("labels") or spec.get("x") or [])]
    if not labels:
        raise ChartError("柱状图需要 labels (bar chart needs labels)")
    ser = _series(spec, len(labels))
    unit = str(spec.get("unit") or "")
    if spec.get("sort") in ("desc", "asc") and len(ser) == 1:
        order = sorted(range(len(labels)), key=lambda i: (ser[0]["values"][i] or 0), reverse=spec["sort"] == "desc")
        labels = [labels[i] for i in order]
        ser[0]["values"] = [ser[0]["values"][i] for i in order]
    allv = [v for s in ser for v in s["values"] if v is not None] + [0]
    ticks = _nice_ticks(min(allv), max(allv))
    lo, hi = ticks[0], ticks[-1]
    multi = len(ser) > 1
    color_each = not multi and bool(spec.get("color_by_sign", any(v < 0 for v in allv)))
    g = []
    if spec.get("horizontal"):
        lw = min(260, max(_tw(x, 13) for x in labels) + 16)
        L, T, R, B = _plot_area(multi, bool(spec.get("subtitle")))
        L = 32 + lw
        R = W - 90
        n = len(labels)
        band = (B - T) / n
        bh = min(34, band * 0.72 / len(ser))
        xs = lambda v: L + (R - L) * (v - lo) / (hi - lo)
        for t in ticks:
            g.append(f'<line x1="{xs(t):.1f}" y1="{T}" x2="{xs(t):.1f}" y2="{B}" stroke="{GRID}"/>'
                     f'<text x="{xs(t):.1f}" y="{B + 18}" font-size="12" fill="{MUTED}" text-anchor="middle">{_e(_fmt(t, unit))}</text>')
        for i, lab in enumerate(labels):
            yc = T + band * (i + 0.5)
            g.append(f'<text x="{L - 10}" y="{yc + 4:.1f}" font-size="13" fill="{INK}" text-anchor="end">{_e(lab)}</text>')
            for k, s in enumerate(ser):
                v = s["values"][i]
                if v is None:
                    continue
                y = yc - bh * len(ser) / 2 + k * bh
                x0, x1 = sorted((xs(0), xs(v)))
                c = ("#16a34a" if v >= 0 else "#dc2626") if color_each else PALETTE[k % len(PALETTE)]
                g.append(f'<rect x="{x0:.1f}" y="{y:.1f}" width="{max(1, x1 - x0):.1f}" height="{bh - 2:.1f}" rx="3" fill="{c}"/>'
                         f'<text x="{(x1 + 6) if v >= 0 else (x0 - 6):.1f}" y="{y + bh / 2 + 3:.1f}" font-size="12" fill="{INK}" '
                         f'text-anchor="{"start" if v >= 0 else "end"}">{_e(_fmt(v, unit))}</text>')
        g.append(f'<line x1="{xs(0):.1f}" y1="{T}" x2="{xs(0):.1f}" y2="{B}" stroke="#9ca3af"/>')
    else:
        L, T, R, B = _plot_area(multi, bool(spec.get("subtitle")))
        n = len(labels)
        band = (R - L) / n
        bw = min(56, band * 0.75 / len(ser))
        ys = lambda v: B - (B - T) * (v - lo) / (hi - lo)
        for t in ticks:
            g.append(f'<line x1="{L}" y1="{ys(t):.1f}" x2="{R}" y2="{ys(t):.1f}" stroke="{GRID}"/>'
                     f'<text x="{L - 8}" y="{ys(t) + 4:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{_e(_fmt(t, unit))}</text>')
        rot = n > 8 or max(_tw(x, 12) for x in labels) > band
        for i, lab in enumerate(labels):
            xc = L + band * (i + 0.5)
            if rot:
                g.append(f'<text x="{xc:.1f}" y="{B + 14}" font-size="12" fill="{INK}" text-anchor="end" '
                         f'transform="rotate(-30 {xc:.1f} {B + 14})">{_e(lab)}</text>')
            else:
                g.append(f'<text x="{xc:.1f}" y="{B + 20}" font-size="12" fill="{INK}" text-anchor="middle">{_e(lab)}</text>')
            for k, s in enumerate(ser):
                v = s["values"][i]
                if v is None:
                    continue
                x = xc - bw * len(ser) / 2 + k * bw
                y0, y1 = sorted((ys(0), ys(v)))
                c = ("#16a34a" if v >= 0 else "#dc2626") if color_each else PALETTE[k % len(PALETTE)]
                g.append(f'<rect x="{x + 1:.1f}" y="{y0:.1f}" width="{bw - 2:.1f}" height="{max(1, y1 - y0):.1f}" rx="3" fill="{c}"/>')
                if n * len(ser) <= 24:
                    ty = (y0 - 6) if v >= 0 else (y1 + 14)
                    g.append(f'<text x="{x + bw / 2:.1f}" y="{ty:.1f}" font-size="11" fill="{INK}" text-anchor="middle">'
                             f'{_e(_fmt(v, unit))}</text>')
        g.append(f'<line x1="{L}" y1="{ys(0):.1f}" x2="{R}" y2="{ys(0):.1f}" stroke="#9ca3af"/>')
    if multi:
        g.append(_legend([s["name"] for s in ser], 78, (92 if spec.get("subtitle") else 76) - 14, W - 120))
    return _frame(spec.get("title", ""), spec.get("subtitle", ""), spec.get("source", ""), "".join(g))


# ----------------------------------------------------------------------------------------------- pie
def pie(spec: dict) -> str:
    labels = [str(x) for x in (spec.get("labels") or [])]
    vals = [_num(v) for v in (spec.get("values") or (spec.get("series") or [{}])[0].get("values") or [])]
    if not labels or len(labels) != len(vals):
        raise ChartError("饼图需要等长的 labels 和 values (pie needs labels and values of the same length)")
    if any(v is None or v < 0 for v in vals) or sum(vals) <= 0:
        raise ChartError("饼图的数值必须是正数 (pie values must be positive)")
    tot = sum(vals)
    cx, cy, r = 330, H / 2 + 14, 175
    inner = r * 0.55 if spec.get("donut", True) else 0
    g, a0 = [], -math.pi / 2
    for i, v in enumerate(vals):
        a1 = a0 + 2 * math.pi * v / tot
        c = PALETTE[i % len(PALETTE)]
        if v / tot >= 0.9999:
            g.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{c}"/>')
        else:
            large = 1 if a1 - a0 > math.pi else 0
            p = (f"M{cx + r * math.cos(a0):.1f},{cy + r * math.sin(a0):.1f} A{r},{r} 0 {large} 1 "
                 f"{cx + r * math.cos(a1):.1f},{cy + r * math.sin(a1):.1f} L{cx},{cy} Z")
            g.append(f'<path d="{p}" fill="{c}" stroke="#fff" stroke-width="2"/>')
        am = (a0 + a1) / 2
        if v / tot >= 0.04:
            rr = (r + inner) / 2 if inner else r * 0.62
            g.append(f'<text x="{cx + rr * math.cos(am):.1f}" y="{cy + rr * math.sin(am) + 5:.1f}" font-size="14" '
                     f'font-weight="700" fill="#fff" text-anchor="middle">{v / tot * 100:.0f}%</text>')
        a0 = a1
    if inner:
        g.append(f'<circle cx="{cx}" cy="{cy}" r="{inner}" fill="#fff"/>')
        if spec.get("center_text"):
            g.append(f'<text x="{cx}" y="{cy + 6}" font-size="18" font-weight="700" fill="{INK}" text-anchor="middle">'
                     f'{_e(spec["center_text"])}</text>')
    unit = str(spec.get("unit") or "")
    lx, ly = 560, cy - len(labels) * 15
    for i, (lab, v) in enumerate(zip(labels, vals)):
        y = ly + i * 30
        g.append(f'<rect x="{lx}" y="{y - 12}" width="16" height="16" rx="3" fill="{PALETTE[i % len(PALETTE)]}"/>'
                 f'<text x="{lx + 26}" y="{y + 1}" font-size="15" fill="{INK}">{_e(lab)}</text>'
                 f'<text x="{W - 40}" y="{y + 1}" font-size="15" fill="{MUTED}" text-anchor="end">'
                 f'{_e(f"{v / tot * 100:.1f}%" if unit == "%" else _fmt(v, unit) + f"  ·  {v / tot * 100:.1f}%")}</text>')
    return _frame(spec.get("title", ""), spec.get("subtitle", ""), spec.get("source", ""), "".join(g))


# ----------------------------------------------------------------------------------------------- gantt
def _date(s) -> _dt.date:
    s = str(s).strip()
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return _dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ChartError(f"日期格式应为 YYYY-MM-DD (date must be YYYY-MM-DD): {s!r}")


def gantt(spec: dict) -> str:
    tasks = spec.get("tasks") or []
    if not tasks:
        raise ChartError("甘特图需要 tasks（[{name, start, end 或 days/weeks}]）")
    rows, prev_end = [], None
    for t in tasks[:30]:
        st = _date(t["start"]) if t.get("start") else (prev_end or _dt.date.today())
        if t.get("end"):
            en = _date(t["end"])
        else:
            days = _num(t.get("days")) or (_num(t.get("weeks")) or 1) * 7
            en = st + _dt.timedelta(days=int(days))
        if en == st:   # a one-day item or a deadline (2026-10-02 R10-04: "due 10/03" gave start = end)
            en = st + _dt.timedelta(days=1)
        if en < st:
            raise ChartError(f"「{t.get('name', '')}」的结束日期要晚于开始日期 (end must be after start)")
        rows.append((str(t.get("name") or ""), st, en))
        prev_end = en
    d0, d1 = min(r[1] for r in rows), max(r[2] for r in rows)
    span = (d1 - d0).days or 1
    lw = min(240, max(_tw(r[0], 13) for r in rows) + 20)
    L, T, R, B = 32 + lw, 116, W - 40, H - 60
    band = min(46, (B - T) / len(rows))
    B = T + band * len(rows)
    xs = lambda d: L + (R - L) * (d - d0).days / span
    g = []
    step = 7 if span <= 120 else 30
    d = d0
    while d <= d1:
        x = xs(d)
        g.append(f'<line x1="{x:.1f}" y1="{T - 6}" x2="{x:.1f}" y2="{B}" stroke="{GRID}"/>'
                 f'<text x="{x:.1f}" y="{T - 12}" font-size="11" fill="{MUTED}" text-anchor="middle">{d.month}/{d.day}</text>')
        d += _dt.timedelta(days=step)
    for i, (name, st, en) in enumerate(rows):
        y = T + band * i
        c = PALETTE[i % len(PALETTE)]
        x0, x1 = xs(st), xs(en)
        g.append(f'<text x="{L - 10}" y="{y + band / 2 + 4:.1f}" font-size="13" fill="{INK}" text-anchor="end">{_e(name)}</text>'
                 f'<rect x="{x0:.1f}" y="{y + band * 0.2:.1f}" width="{x1 - x0:.1f}" height="{band * 0.6:.1f}" rx="4" fill="{c}"/>')
        lab = f"{st.month}/{st.day}–{en.month}/{en.day} · {(en - st).days}天"
        if x1 - x0 > _tw(lab, 11) + 10:
            g.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{y + band / 2 + 4:.1f}" font-size="11" fill="#fff" '
                     f'text-anchor="middle">{_e(lab)}</text>')
        else:
            g.append(f'<text x="{x1 + 6:.1f}" y="{y + band / 2 + 4:.1f}" font-size="11" fill="{MUTED}">{_e(lab)}</text>')
    h = int(B + 60)
    sub = spec.get("subtitle") or f"{d0.isoformat()} → {d1.isoformat()}（共 {span} 天 {span} days）"
    return _frame(spec.get("title", ""), sub, spec.get("source", ""), "".join(g), h=max(h, 300))


# ----------------------------------------------------------------------------------------------- flow
def _wrap(s: str, width: float, size: float) -> list[str]:
    out, cur = [], ""
    for tok in re.findall(r"[⺀-￿]|[^\s⺀-￿]+|\s+", str(s)):
        if _tw(cur + tok, size) > width and cur.strip():
            out.append(cur.strip())
            cur = tok.lstrip()
        else:
            cur += tok
    if cur.strip():
        out.append(cur.strip())
    return out[:4] or [""]


def flow(spec: dict) -> str:
    nodes = spec.get("nodes") or []
    edges = spec.get("edges") or []
    if not nodes and spec.get("steps"):
        nodes = [{"id": f"s{i}", "label": s} for i, s in enumerate(spec["steps"])]
        edges = [{"from": f"s{i}", "to": f"s{i + 1}"} for i in range(len(nodes) - 1)]
    if not nodes:
        raise ChartError("流程图需要 nodes（[{id, label}]）和 edges（[{from, to, label}]），或者 steps（[...]）")
    ids, lab = [], {}
    for i, n in enumerate(nodes[:40]):
        if isinstance(n, dict):
            nid = str(n["id"]) if n.get("id") is not None else str(n.get("label") or f"n{i}")
            lab[nid] = str(n.get("label") or nid)
        else:
            nid = lab_v = str(n)
            lab[nid] = lab_v
        if nid not in ids:
            ids.append(nid)
    E = []
    for e in edges[:80]:
        a, b = str(e.get("from")), str(e.get("to"))
        if a not in lab or b not in lab:
            raise ChartError(f"连线指向不存在的节点 edge refers to an unknown node: {a} → {b}")
        E.append((a, b, str(e.get("label") or "")))
    # layers = longest path from the sources, after dropping back edges (loops such as "rejected → resubmit")
    adj = {n: [b for a, b, _l in E if a == n] for n in ids}
    state, back = {}, set()

    def dfs(u):
        state[u] = 1
        for v in adj[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif not state.get(v):
                dfs(v)
        state[u] = 2
    for n in ids:
        if not state.get(n):
            dfs(n)
    layer = {n: 0 for n in ids}
    for _ in range(len(ids)):
        for a, b, _l in E:
            if (a, b) not in back and layer[b] < layer[a] + 1:
                layer[b] = layer[a] + 1
    nl = max(layer.values()) + 1
    groups = [[n for n in ids if layer[n] == k] for k in range(nl)]
    lr = str(spec.get("direction") or "").upper() == "LR" or (nl <= 6 and max(len(g) for g in groups) <= 2
                                                                and str(spec.get("direction") or "").upper() != "TB")
    bw, bh = 170, 64
    if lr:
        gapx = max(60, min(110, (W - 80 - nl * bw) / max(1, nl - 1)))
        w = int(max(W, 80 + nl * bw + (nl - 1) * gapx))
        maxg = max(len(g) for g in groups)
        h = int(max(420, 260 + maxg * (bh + 46)))
        pos = {}
        for k, g in enumerate(groups):
            tot = len(g) * bh + (len(g) - 1) * 46
            for j, n in enumerate(g):
                pos[n] = (40 + k * (bw + gapx), (h - 40 - tot) / 2 + 40 + j * (bh + 46))
    else:
        gapy = 56
        maxg = max(len(g) for g in groups)
        w = int(max(W, 160 + maxg * (bw + 40)))
        h = int(120 + nl * (bh + gapy))
        pos = {}
        for k, g in enumerate(groups):
            tot = len(g) * bw + (len(g) - 1) * 40
            for j, n in enumerate(g):
                pos[n] = ((w - tot) / 2 + j * (bw + 40), 90 + k * (bh + gapy))
    defs = ('<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>')
    g = [defs]
    top = min(y for _x, y in pos.values())
    low = max(y for _x, y in pos.values()) + bh   # lowest box edge (LR) / rightmost box edge (TB) for detours
    right = max(x for x, _y in pos.values()) + bw
    for a, b, l in E:
        (xa, ya), (xb, yb) = pos[a], pos[b]
        span = layer[b] - layer[a]
        if lr:
            if span == 1 and xb > xa:   # neighbouring columns: straight-ish S curve
                p0, p3 = (xa + bw, ya + bh / 2), (xb - 2, yb + bh / 2)
                mx = (p0[0] + p3[0]) / 2
                p1, p2 = (mx, p0[1]), (mx, p3[1])
            else:                        # skips columns or goes back: detour below the boxes, or above them when
                below = any((layer[n2] == layer[a] and pos[n2][1] > ya) or (layer[n2] == layer[b] and pos[n2][1] > yb)
                            for n2 in ids if n2 not in (a, b))   # a box sits under the start/end in its column
                if below:
                    p0, p3 = (xa + bw / 2, ya), (xb + bw / 2, yb - 2)
                    dip = top - 36 - 14 * abs(span)
                else:
                    p0, p3 = (xa + bw / 2, ya + bh), (xb + bw / 2, yb + bh + 2)
                    dip = low + 36 + 14 * abs(span)
                p1, p2 = (p0[0], dip), (p3[0], dip)
        else:
            if span == 1 and yb > ya:
                p0, p3 = (xa + bw / 2, ya + bh), (xb + bw / 2, yb - 2)
                my = (p0[1] + p3[1]) / 2
                p1, p2 = (p0[0], my), (p3[0], my)
            else:                        # detour to the right of the boxes
                p0, p3 = (xa + bw, ya + bh / 2), (xb + bw + 2, yb + bh / 2)
                out = right + 36 + 14 * abs(span)
                p1, p2 = (out, p0[1]), (out, p3[1])
        d = (f"M{p0[0]:.1f},{p0[1]:.1f} C{p1[0]:.1f},{p1[1]:.1f} {p2[0]:.1f},{p2[1]:.1f} {p3[0]:.1f},{p3[1]:.1f}")
        g.append(f'<path d="{d}" fill="none" stroke="{MUTED}" stroke-width="1.8" marker-end="url(#arr)"/>')
        if l:   # label at the curve's midpoint
            mx = (p0[0] + 3 * p1[0] + 3 * p2[0] + p3[0]) / 8
            my = (p0[1] + 3 * p1[1] + 3 * p2[1] + p3[1]) / 8
            tw = _tw(l, 11) + 10
            g.append(f'<rect x="{mx - tw / 2:.1f}" y="{my - 10:.1f}" width="{tw:.1f}" height="18" rx="4" fill="#fff" stroke="{GRID}"/>'
                     f'<text x="{mx:.1f}" y="{my + 3:.1f}" font-size="11" fill="{MUTED}" text-anchor="middle">{_e(l)}</text>')
    for i, n in enumerate(ids):
        x, y = pos[n]
        c = PALETTE[layer[n] % len(PALETTE)]
        lines = _wrap(lab[n], bw - 18, 14)
        g.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw}" height="{bh}" rx="10" fill="#fff" stroke="{c}" stroke-width="2"/>'
                 f'<rect x="{x:.1f}" y="{y:.1f}" width="6" height="{bh}" rx="3" fill="{c}"/>')
        y0 = y + bh / 2 - (len(lines) - 1) * 9 + 5
        for k, ln in enumerate(lines):
            g.append(f'<text x="{x + bw / 2 + 3:.1f}" y="{y0 + k * 18:.1f}" font-size="14" fill="{INK}" text-anchor="middle">{_e(ln)}</text>')
    return _frame(spec.get("title", ""), spec.get("subtitle", ""), spec.get("source", ""), "".join(g), w=w, h=h)


def render(spec: dict) -> str:
    """SVG text for a chart spec ({type, title, ...}); raises ChartError with a message the agent can act on."""
    kind = str(spec.get("type") or "").lower().strip()
    kind = {"hbar": "bar", "column": "bar", "ranking": "bar", "donut": "pie", "flowchart": "flow", "diagram": "flow",
            "timeline": "gantt", "area": "line", "trend": "line"}.get(kind, kind)
    if kind == "bar" and str(spec.get("type")).lower() in ("hbar", "ranking"):
        spec = {**spec, "horizontal": True}
    if kind not in TYPES:
        raise ChartError(f"type 应为 {', '.join(TYPES)} 之一 (type must be one of {', '.join(TYPES)})")
    return {"line": line, "bar": bar, "pie": pie, "gantt": gantt, "flow": flow}[kind](spec)
