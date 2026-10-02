"""Safe calculator for the agent (no code execution): arithmetic, math functions and a few finance helpers.

The model is bad at arithmetic (a 2026-10-02 test answered a S$3M / 25-year / 3.5% mortgage with S$15,155 a month and a
first-month interest of S$1,761; the right numbers are S$15,018.71 and S$8,750). Expressions are parsed with `ast` and
only numbers, names from the whitelist below, the user's own variables and arithmetic operators are allowed."""
from __future__ import annotations

import ast
import math
import operator
import statistics

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UN = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq,
        ast.NotEq: operator.ne}


class CalcError(Exception):
    pass


def pmt(rate: float, nper: float, pv: float, fv: float = 0.0) -> float:
    """Payment per period for a loan (positive number), like Excel PMT without the sign."""
    if rate == 0:
        return (pv + fv) / nper
    return (pv * (1 + rate) ** nper + fv) * rate / ((1 + rate) ** nper - 1)


def loan(principal: float, annual_rate_pct: float, years: float, rows: int = 12, per_year: int = 12) -> dict:
    """Level-payment loan: payment, totals and the first `rows` lines of the amortization schedule."""
    r, n = annual_rate_pct / 100 / per_year, int(round(years * per_year))
    pay = pmt(r, n, principal)
    bal, sched = float(principal), []
    for i in range(1, min(int(rows), n, 600) + 1):
        it = bal * r
        pr = pay - it
        bal -= pr
        sched.append({"period": i, "payment": round(pay, 2), "principal": round(pr, 2), "interest": round(it, 2),
                      "balance": round(max(bal, 0.0), 2)})
    return {"payment": round(pay, 2), "periods": n, "total_paid": round(pay * n, 2),
            "total_interest": round(pay * n - principal, 2), "schedule": sched}


def fv(rate: float, nper: float, pmt_: float = 0.0, pv: float = 0.0) -> float:
    """Future value of a lump sum `pv` plus regular deposits `pmt_` (positive numbers)."""
    if rate == 0:
        return pv + pmt_ * nper
    g = (1 + rate) ** nper
    return pv * g + pmt_ * (g - 1) / rate


def cagr(start: float, end: float, years: float) -> float:
    """Compound annual growth rate in percent."""
    return ((end / start) ** (1 / years) - 1) * 100


FUNCS = {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "len": len, "int": int, "float": float,
         "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "ceil": math.ceil, "floor": math.floor,
         "mean": statistics.fmean, "median": statistics.median, "stdev": statistics.stdev,
         "pmt": pmt, "fv": fv, "cagr": cagr, "loan": loan}
CONSTS = {"pi": math.pi, "e": math.e}


def _ev(node, env: dict):
    if isinstance(node, ast.Expression):
        return _ev(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in CONSTS:
            return CONSTS[node.id]
        raise CalcError(f"unknown name {node.id!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _ev(node.left, env), _ev(node.right, env)
        if not all(isinstance(x, (int, float)) for x in (a, b)):
            raise CalcError("arithmetic works on numbers only")
        if isinstance(node.op, ast.Pow) and abs(b) > 10000:
            raise CalcError("exponent too large")
        return _BIN[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_ev(node.operand, env))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
        return _CMP[type(node.ops[0])](_ev(node.left, env), _ev(node.comparators[0], env))
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > 10000:
            raise CalcError("list too long")
        return [_ev(x, env) for x in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
        return FUNCS[node.func.id](*[_ev(a, env) for a in node.args])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS:
        return FUNCS[node.func.id](*[_ev(a, env) for a in node.args], **{k.arg: _ev(k.value, env) for k in node.keywords})
    raise CalcError(f"not allowed: {ast.dump(node)[:60]}")


def evaluate(expr: str, env: dict | None = None):
    s = str(expr or "").strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace("，", ",")
    if not s or len(s) > 2000:
        raise CalcError("empty or too long expression")
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"syntax error: {e.msg}")
    try:
        return _ev(tree, env or {})
    except CalcError:
        raise
    except (ZeroDivisionError, OverflowError, ValueError, TypeError, statistics.StatisticsError) as e:
        raise CalcError(f"{type(e).__name__}: {e}")


def run(expressions, variables: dict | None = None) -> list[tuple[str, object]]:
    """Evaluate variables in order (each may use the earlier ones), then each expression."""
    env: dict = {}
    for k, v in (variables or {}).items():
        if not str(k).isidentifier() or k in FUNCS:
            raise CalcError(f"bad variable name {k!r}")
        env[k] = evaluate(v, env) if isinstance(v, str) else v
    if isinstance(expressions, str):
        expressions = [expressions]
    out = []
    for x in list(expressions or [])[:50]:
        try:
            out.append((str(x), evaluate(x, env)))
        except CalcError as e:
            out.append((str(x), f"ERROR: {e}"))
    return out


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else f"{v:.6g}"
    if isinstance(v, dict) and "schedule" in v:
        head = (f"payment {v['payment']:,.2f} × {v['periods']} periods; total paid {v['total_paid']:,.2f}; "
                f"total interest {v['total_interest']:,.2f}")
        rows = "\n".join(f"| {r['period']} | {r['payment']:,.2f} | {r['principal']:,.2f} | {r['interest']:,.2f} | {r['balance']:,.2f} |"
                         for r in v["schedule"])
        return head + "\n| # | payment | principal | interest | balance |\n|---|---|---|---|---|\n" + rows
    return str(v)
