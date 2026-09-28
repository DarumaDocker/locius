"""Sentinel persistent state: approvals, grants, taint, connections, secrets, audit (hash chained)."""
from __future__ import annotations

import hashlib
import json
import os
import threading

from cryptography.fernet import Fernet

from app.common.util import DB, dumps, loads, new_id, now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, task_id TEXT, call_id TEXT, tool TEXT, args TEXT, summary TEXT,
  risk TEXT, reason TEXT, status TEXT, scope TEXT, decided_by TEXT,
  result TEXT, created_at REAL, resolved_at REAL
);
CREATE INDEX IF NOT EXISTS ix_appr_status ON approvals(status);
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, tool TEXT, scope TEXT, task_id TEXT, match TEXT,
  expires_at REAL, created_at REAL, revoked INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS task_ctx (
  task_id TEXT PRIMARY KEY, taint TEXT, injection TEXT, domains TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS connections (
  name TEXT PRIMARY KEY, config TEXT, permissions TEXT, enabled INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS secrets (
  handle TEXT PRIMARY KEY, connector TEXT, blob BLOB, created_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, task_id TEXT, action TEXT,
  resource TEXT, risk TEXT, decision TEXT, result TEXT, detail TEXT, prev_hash TEXT, hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_task ON audit(task_id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, updated_at REAL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""

DEFAULT_CONNECTIONS = {
    "gmail": {
        "config": {"email": "", "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com", "display_name": ""},
        "permissions": {"read": True, "organize": True, "draft": True, "send": True},
        "enabled": 1,
    },
    "browser": {
        "config": {"blocked_domains": [], "allowed_domains": []},
        "permissions": {"browse": True, "interact": True, "upload": True, "download": True},
        "enabled": 1,
    },
    "telegram": {
        "config": {"chat_id": ""},
        "permissions": {"notify": True, "control": True},
        "enabled": 0,
    },
    "notion": {
        "config": {"workspace": "", "bot_id": ""},
        "permissions": {"read": True, "write": True},
        "enabled": 0,
    },
    "slack": {
        "config": {"team": "", "user": "", "user_id": "", "bot_id": "", "token_type": ""},
        "permissions": {"read": True, "send": True},
        "enabled": 0,
    },
}


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.dir = data_dir
        self.db = DB(os.path.join(data_dir, "sentinel.db"))
        self.db.script(SCHEMA)
        self._audit_lock = threading.Lock()
        self._jsonl = os.path.join(data_dir, "audit.jsonl")
        self._fernet = Fernet(self._load_key())
        for name, d in DEFAULT_CONNECTIONS.items():
            if not self.db.one("SELECT name FROM connections WHERE name=?", (name,)):
                self.db.insert("connections", {
                    "name": name, "config": dumps(d["config"]), "permissions": dumps(d["permissions"]),
                    "enabled": d["enabled"], "updated_at": now_ts(),
                })

    # ------------------------------------------------------------ vault
    def _load_key(self) -> bytes:
        path = os.path.join(self.dir, "vault.key")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read().strip()
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key

    def put_secret(self, connector: str, payload: dict, handle: str | None = None) -> str:
        handle = handle or f"cred_{connector}_1"
        blob = self._fernet.encrypt(json.dumps(payload).encode())
        self.db.execute("INSERT OR REPLACE INTO secrets(handle, connector, blob, created_at) VALUES (?,?,?,?)",
                        (handle, connector, blob, now_ts()))
        return handle

    def get_secret(self, handle: str) -> dict | None:
        row = self.db.one("SELECT blob FROM secrets WHERE handle=?", (handle,))
        if not row:
            return None
        try:
            return json.loads(self._fernet.decrypt(row["blob"]))
        except Exception:
            return None

    def has_secret(self, handle: str) -> bool:
        return self.db.one("SELECT handle FROM secrets WHERE handle=?", (handle,)) is not None

    def delete_secret(self, handle: str) -> None:
        self.db.execute("DELETE FROM secrets WHERE handle=?", (handle,))

    # ------------------------------------------------------------ small key/value state (bot offsets, public URL, ...)
    def kv_get(self, k: str, default=None):
        row = self.db.one("SELECT v FROM kv WHERE k=?", (k,))
        return loads(row["v"], default) if row else default

    def kv_set(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(k, v, updated_at) VALUES (?,?,?)", (k, dumps(v), now_ts()))

    # ------------------------------------------------------------ connections
    def connection(self, name: str) -> dict:
        row = self.db.one("SELECT * FROM connections WHERE name=?", (name,))
        if not row:
            return {"name": name, "config": {}, "permissions": {}, "enabled": False}
        return {"name": name, "config": loads(row["config"], {}), "permissions": loads(row["permissions"], {}),
                "enabled": bool(row["enabled"]), "updated_at": row["updated_at"]}

    def save_connection(self, name: str, config: dict | None = None, permissions: dict | None = None,
                        enabled: bool | None = None) -> dict:
        cur = self.connection(name)
        if config is not None:
            cur["config"].update(config)
        if permissions is not None:
            cur["permissions"].update({k: bool(v) for k, v in permissions.items()})
        if enabled is not None:
            cur["enabled"] = bool(enabled)
        self.db.execute(
            "INSERT OR REPLACE INTO connections(name, config, permissions, enabled, updated_at) VALUES (?,?,?,?,?)",
            (name, dumps(cur["config"]), dumps(cur["permissions"]), int(cur["enabled"]), now_ts()))
        return self.connection(name)

    # ------------------------------------------------------------ task context (taint / injection)
    def task_ctx(self, task_id: str) -> dict:
        row = self.db.one("SELECT * FROM task_ctx WHERE task_id=?", (task_id or "",))
        if not row:
            return {"taint": "PUBLIC", "injection": [], "domains": []}
        return {"taint": row["taint"], "injection": loads(row["injection"], []), "domains": loads(row["domains"], [])}

    def update_task_ctx(self, task_id: str, taint: str | None = None, injection: list | None = None,
                        domain: str | None = None) -> dict:
        from app.sentinel.guard import max_class
        ctx = self.task_ctx(task_id)
        if taint:
            ctx["taint"] = max_class(ctx["taint"], taint)
        if injection:
            ctx["injection"] = sorted(set(ctx["injection"]) | set(injection))
        if domain and domain not in ctx["domains"]:
            ctx["domains"].append(domain)
        self.db.execute("INSERT OR REPLACE INTO task_ctx(task_id, taint, injection, domains, updated_at) VALUES (?,?,?,?,?)",
                        (task_id or "", ctx["taint"], dumps(ctx["injection"]), dumps(ctx["domains"]), now_ts()))
        return ctx

    # ------------------------------------------------------------ grants
    def add_grant(self, tool: str, scope: str, task_id: str | None, match: dict | None, ttl: float | None,
                  note: str = "") -> str:
        gid = new_id("grant")
        self.db.insert("grants", {
            "id": gid, "tool": tool, "scope": scope, "task_id": task_id or "", "match": dumps(match or {}),
            "expires_at": (now_ts() + ttl) if ttl else None, "created_at": now_ts(), "revoked": 0, "note": note,
        })
        return gid

    def active_grants(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM grants WHERE revoked=0 ORDER BY created_at DESC")
        out = []
        for r in rows:
            if r["expires_at"] and r["expires_at"] < now_ts():
                continue
            r["match"] = loads(r["match"], {})
            out.append(r)
        return out

    def revoke_grant(self, gid: str) -> None:
        self.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (gid,))

    # ------------------------------------------------------------ approvals
    def create_approval(self, task_id: str, call_id: str, tool: str, args: dict, summary: dict, risk: str,
                        reason: str) -> dict:
        aid = new_id("appr")
        self.db.insert("approvals", {
            "id": aid, "task_id": task_id, "call_id": call_id, "tool": tool, "args": dumps(args),
            "summary": dumps(summary), "risk": risk, "reason": reason, "status": "pending", "scope": "",
            "decided_by": "", "result": "", "created_at": now_ts(), "resolved_at": None,
        })
        return self.approval(aid)

    def approval(self, aid: str) -> dict | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        if not r:
            return None
        r["args"] = loads(r["args"], {})
        r["summary"] = loads(r["summary"], {})
        r["result"] = loads(r["result"], None)
        return r

    def approvals(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status == "resolved":   # history: everything already decided (approved / denied / expired)
            rows = self.db.all("SELECT id FROM approvals WHERE status!='pending' ORDER BY COALESCE(resolved_at, created_at) DESC "
                               "LIMIT ?", (limit,))
            return [self.approval(r["id"]) for r in rows]
        if status:
            rows = self.db.all("SELECT id FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.all("SELECT id FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.approval(r["id"]) for r in rows]

    def resolve_approval(self, aid: str, status: str, scope: str, result=None, args: dict | None = None,
                         decided_by: str = "user") -> None:
        data = {"status": status, "scope": scope, "resolved_at": now_ts(), "decided_by": decided_by,
                "result": dumps(result) if result is not None else ""}
        if args is not None:
            data["args"] = dumps(args)
        self.db.update("approvals", "id", aid, data)

    # ------------------------------------------------------------ audit (append-only, hash chained)
    def audit(self, actor: str, action: str, *, task_id: str = "", resource: str = "", risk: str = "",
              decision: str = "", result: str = "", detail: dict | None = None) -> dict:
        with self._audit_lock:
            last = self.db.one("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1")
            prev = last["hash"] if last else "GENESIS"
            ts = now_ts()
            detail_s = dumps(detail or {})
            if len(detail_s) > 20000:
                detail_s = dumps({"truncated": True, "preview": detail_s[:20000]})
            body = dumps([ts, actor, task_id, action, resource, risk, decision, result, detail_s, prev])
            h = hashlib.sha256(body.encode()).hexdigest()
            seq = self.db.insert("audit", {
                "ts": ts, "actor": actor, "task_id": task_id, "action": action, "resource": resource, "risk": risk,
                "decision": decision, "result": result, "detail": detail_s, "prev_hash": prev, "hash": h,
            })
            rec = {"seq": seq, "ts": ts, "actor": actor, "task_id": task_id, "action": action, "resource": resource,
                   "risk": risk, "decision": decision, "result": result, "detail": loads(detail_s, {}),
                   "prev_hash": prev, "hash": h}
            try:
                with open(self._jsonl, "a", encoding="utf-8") as f:
                    f.write(dumps(rec) + "\n")
            except Exception:
                pass
            return rec

    def audit_list(self, task_id: str | None = None, limit: int = 200, before: int | None = None,
                   actor: str | None = None) -> list[dict]:
        q = "SELECT * FROM audit WHERE 1=1"
        p: list = []
        if task_id:
            q += " AND task_id=?"
            p.append(task_id)
        if actor:
            q += " AND actor=?"
            p.append(actor)
        if before:
            q += " AND seq<?"
            p.append(before)
        q += " ORDER BY seq DESC LIMIT ?"
        p.append(limit)
        rows = self.db.all(q, p)
        for r in rows:
            r["detail"] = loads(r["detail"], {})
        return rows

    def audit_verify(self) -> dict:
        prev = "GENESIS"
        n = 0
        for r in self.db.all("SELECT * FROM audit ORDER BY seq ASC"):
            body = dumps([r["ts"], r["actor"], r["task_id"], r["action"], r["resource"], r["risk"], r["decision"],
                          r["result"], r["detail"], prev])
            if r["prev_hash"] != prev or hashlib.sha256(body.encode()).hexdigest() != r["hash"]:
                return {"ok": False, "broken_at": r["seq"], "checked": n}
            prev = r["hash"]
            n += 1
        return {"ok": True, "checked": n, "head": prev}
