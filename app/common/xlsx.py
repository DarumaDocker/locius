"""Excel (.xlsx) files made and read locally with openpyxl — nothing leaves the box.

make(): sheets of rows → a tidy workbook (bold header, frozen top row, filter, sensible column widths,
numbers stored as numbers). Formulas are only kept when the caller asks for them, and never functions that reach
the network or other programs (text copied from web pages must not become a live formula).
read_text(): a workbook → plain text (one Markdown-ish table per sheet) so the agent can read .xlsx files.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata

MAX_ROWS = 20000
MAX_COLS = 200
_NUM = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_PCT = re.compile(r"^[+-]?\d+(\.\d+)?%$")
_BAD_FORMULA = re.compile(r"(WEBSERVICE|HYPERLINK|IMPORT\w*|FILTERXML|CALL|REGISTER|EXEC|DDE|RTD|INDIRECT)\s*\(|\||!\w+\(", re.I)


class XlsxError(Exception):
    pass


def _width(v) -> int:
    s = str(v if v is not None else "")
    line = max(s.split("\n"), key=len) if s else ""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in line)


def _cell_value(v, formulas: bool):
    """Numbers stay numbers; text that looks like a formula stays text unless formulas are allowed and safe."""
    if v is None or isinstance(v, bool):
        return v, None
    if isinstance(v, (int, float)):
        return v, None
    s = str(v)
    t = s.strip()
    if _NUM.match(t):
        n = float(t.replace(",", ""))
        return (int(n) if n.is_integer() and "." not in t else n), None
    if _PCT.match(t):
        return float(t[:-1]) / 100, "0.0%"
    if t[:1] in ("=", "+", "-", "@") and not _NUM.match(t):
        if formulas and t.startswith("=") and not _BAD_FORMULA.search(t):
            return t, None
        return s, "text"           # stored as text, never evaluated
    return s, None


def rows_from_csv(text: str) -> list[list]:
    return [r for r in csv.reader(io.StringIO(text))]


def rows_from_markdown(text: str) -> list[list]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append([re.sub(r"\*\*(.+?)\*\*", r"\1", c) for c in cells])
    return rows


def make(path: str, sheets: list[dict], formulas: bool = False) -> dict:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    if not sheets:
        raise XlsxError("没有数据 no sheets/rows given")
    wb = Workbook()
    wb.remove(wb.active)
    used, total = set(), 0
    for i, sh in enumerate(sheets):
        name = re.sub(r"[\[\]:*?/\\]", "_", str(sh.get("name") or f"Sheet{i + 1}"))[:31] or f"Sheet{i + 1}"
        while name in used:
            name = (name[:28] + f"_{i + 1}")[:31]
        used.add(name)
        ws = wb.create_sheet(name)
        columns = list(sh.get("columns") or [])
        rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in (sh.get("rows") or [])]
        if rows and isinstance(sh.get("rows")[0], dict):
            columns = columns or list(sh["rows"][0].keys())
            rows = [[d.get(c) for c in columns] for d in sh["rows"]]
        if len(rows) > MAX_ROWS:
            raise XlsxError(f"行数太多 too many rows (> {MAX_ROWS})")
        widths: dict[int, int] = {}
        r0 = 1
        if columns:
            for j, c in enumerate(columns[:MAX_COLS], 1):
                cell = ws.cell(row=1, column=j, value=str(c))
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F6F5C")
                cell.alignment = Alignment(vertical="center", wrap_text=True)
                widths[j] = max(widths.get(j, 0), _width(c))
            r0 = 2
        for ri, row in enumerate(rows, r0):
            for j, v in enumerate(row[:MAX_COLS], 1):
                val, fmt = _cell_value(v, formulas)
                cell = ws.cell(row=ri, column=j)
                if fmt == "text":
                    cell.value = val
                    cell.data_type = "s"
                else:
                    cell.value = val
                    if fmt:
                        cell.number_format = fmt
                if isinstance(val, str) and ("\n" in val or len(val) > 60):
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                widths[j] = max(widths.get(j, 0), min(_width(val), 60))
        for j, w in widths.items():
            ws.column_dimensions[get_column_letter(j)].width = max(8, min(w + 2, 62))
        if columns:
            ws.freeze_panes = "A2"
            if rows:
                ws.auto_filter.ref = f"A1:{get_column_letter(len(columns[:MAX_COLS]))}{len(rows) + 1}"
        total += len(rows)
    wb.save(path)
    return {"sheets": [s for s in wb.sheetnames], "rows": total}


def read_text(path: str, limit: int = 12000) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## Sheet: {ws.title}")
        n = 0
        for row in ws.iter_rows(values_only=True):
            if row is None or all(v is None for v in row):
                continue
            out.append("| " + " | ".join("" if v is None else str(v).replace("\n", " ") for v in row) + " |")
            n += 1
            if sum(len(x) for x in out) > limit:
                out.append("…[已截断 truncated]")
                return "\n".join(out)
        if n == 0:
            out.append("(空 empty)")
    return "\n".join(out)
