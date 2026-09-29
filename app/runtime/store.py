"""Runtime persistent state: conversations, tasks, events, schedules, memory, settings."""
from __future__ import annotations

import os
import re

from app.common.util import DB, dumps, loads, new_id, now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT, kind TEXT DEFAULT 'chat', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, content TEXT, task_id TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conv_id);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, conv_id TEXT, goal TEXT, status TEXT, plan TEXT, transcript TEXT, pending TEXT,
  result TEXT, error TEXT, source TEXT, schedule_id TEXT, parent_id TEXT, steps INTEGER DEFAULT 0,
  waiting TEXT, created_at REAL, updated_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_task_status ON tasks(status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL, type TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_task ON events(task_id);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, name TEXT, goal TEXT, kind TEXT, spec TEXT, tz TEXT, enabled INTEGER,
  last_run REAL, next_run REAL, state TEXT, conv_id TEXT, created_at REAL, last_task TEXT
);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, fact TEXT, category TEXT, entity TEXT, source TEXT, confidence REAL,
  created_at REAL, last_verified REAL, ttl_days INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(id UNINDEXED, fact, entity, tokenize='trigram');
CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, type TEXT, name TEXT, attrs TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS relations (src TEXT, rel TEXT, dst TEXT, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY, title TEXT, objective TEXT, criteria TEXT, status TEXT, deadline REAL, schedule_id TEXT,
  conv_id TEXT, progress TEXT, result TEXT, created_at REAL, updated_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, title TEXT, body TEXT, task_id TEXT, level TEXT, read INTEGER DEFAULT 0
);
"""

DEFAULT_SETTINGS = {
    "model_base_url": os.environ.get("PERSONA_MODEL_URL", ""),
    "model_name": os.environ.get("PERSONA_MODEL", "Olares/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"),
    "planner_model": "",
    "vision_model": "",       # "" = the executor model (Qwen3.x on Olares can read images)
    "temperature": 0.3,
    "max_steps": 40,
    "max_tokens": 4096,
    "timezone": os.environ.get("TZ", "Asia/Singapore"),
    "user_name": "",
    "language": "",          # "" = not chosen yet: the web UI fills it from the browser language on first visit
    "memory_extraction": True,
    "disable_thinking": False,
    "extra_body": "",
    "llm_timeout": 600,
}


class RStore:
    def __init__(self, data_dir: str):
        self.db = DB(os.path.join(data_dir, "runtime.db"))
        try:
            self.db.script(SCHEMA)
        except Exception:
            # sqlite without trigram tokenizer: fall back to unicode61
            self.db.script(SCHEMA.replace("tokenize='trigram'", "tokenize='unicode61'"))
        cols = {r["name"] for r in self.db.all("PRAGMA table_info(schedules)")}
        if "goal_id" not in cols:
            self.db.execute("ALTER TABLE schedules ADD COLUMN goal_id TEXT DEFAULT ''")

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        for r in self.db.all("SELECT key, value FROM settings"):
            s[r["key"]] = loads(r["value"], r["value"])
        return s

    def set_settings(self, d: dict) -> dict:
        for k, v in d.items():
            if k in DEFAULT_SETTINGS:
                self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, dumps(v)))
        return self.settings()

    # ------------------------------------------------------------ conversations
    def create_conv(self, title: str, kind: str = "chat", cid: str | None = None) -> str:
        cid = cid or new_id("conv")
        self.db.execute("INSERT OR IGNORE INTO conversations(id, title, kind, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (cid, title[:80], kind, now_ts(), now_ts()))
        return cid

    def convs(self, limit=100) -> list[dict]:
        return self.db.all("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,))

    def conv(self, cid: str) -> dict | None:
        return self.db.one("SELECT * FROM conversations WHERE id=?", (cid,))

    def add_msg(self, cid: str, role: str, content: str, task_id: str = "") -> int:
        mid = self.db.insert("messages", {"conv_id": cid, "role": role, "content": content, "task_id": task_id,
                                          "created_at": now_ts()})
        self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_ts(), cid))
        return mid

    def msgs(self, cid: str, limit=200) -> list[dict]:
        rows = self.db.all("SELECT * FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        return list(reversed(rows))

    def delete_conv(self, cid: str):
        self.db.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
        self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    # ------------------------------------------------------------ tasks
    def create_task(self, goal: str, conv_id: str, source: str = "chat", schedule_id: str = "", parent_id: str = "") -> dict:
        tid = new_id("task")
        self.db.insert("tasks", {
            "id": tid, "conv_id": conv_id, "goal": goal, "status": "CREATED", "plan": dumps({}), "transcript": dumps([]),
            "pending": dumps(None), "result": "", "error": "", "source": source, "schedule_id": schedule_id,
            "parent_id": parent_id, "steps": 0, "waiting": dumps(None), "created_at": now_ts(), "updated_at": now_ts(),
            "finished_at": None,
        })
        return self.task(tid)

    def task(self, tid: str) -> dict | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not r:
            return None
        for k, d in (("plan", {}), ("transcript", []), ("pending", None), ("waiting", None)):
            r[k] = loads(r[k], d)
        return r

    def update_task(self, tid: str, **kw):
        data = {}
        for k, v in kw.items():
            data[k] = dumps(v) if k in ("plan", "transcript", "pending", "waiting") else v
        data["updated_at"] = now_ts()
        self.db.update("tasks", "id", tid, data)

    def tasks(self, status: str | None = None, limit=100, conv_id: str | None = None) -> list[dict]:
        q = "SELECT id, conv_id, goal, status, plan, result, error, source, schedule_id, parent_id, steps, waiting, created_at, updated_at, finished_at FROM tasks WHERE parent_id=''"
        p: list = []
        if status:
            q += " AND status IN (%s)" % ",".join("?" for _ in status.split(","))
            p += status.split(",")
        if conv_id:
            q += " AND conv_id=?"
            p.append(conv_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        p.append(limit)
        rows = self.db.all(q, p)
        for r in rows:
            r["plan"] = loads(r["plan"], {})
            r["waiting"] = loads(r["waiting"], None)
        return rows

    # ------------------------------------------------------------ events
    def add_event(self, task_id: str, type_: str, data: dict) -> dict:
        ts = now_ts()
        eid = self.db.insert("events", {"task_id": task_id, "ts": ts, "type": type_, "data": dumps(data)})
        return {"id": eid, "task_id": task_id, "ts": ts, "type": type_, "data": data}

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        rows = self.db.all("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, after))
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    # ------------------------------------------------------------ memory
    def add_fact(self, fact: str, category: str = "general", entity: str = "", source: str = "user",
                 confidence: float = 0.9, ttl_days: int | None = None) -> dict | None:
        fact = fact.strip()
        if not fact:
            return None
        norm = re.sub(r"\W+", "", fact.lower())
        for r in self.db.all("SELECT id, fact FROM facts"):
            if re.sub(r"\W+", "", r["fact"].lower()) == norm:
                self.db.execute("UPDATE facts SET last_verified=? WHERE id=?", (now_ts(), r["id"]))
                return {"id": r["id"], "fact": r["fact"], "duplicate": True}
        fid = new_id("fact")
        self.db.insert("facts", {"id": fid, "fact": fact, "category": category, "entity": entity, "source": source,
                                 "confidence": confidence, "created_at": now_ts(), "last_verified": now_ts(),
                                 "ttl_days": ttl_days})
        self.db.execute("INSERT INTO facts_fts(id, fact, entity) VALUES (?,?,?)", (fid, fact, entity))
        return {"id": fid, "fact": fact}

    def delete_fact(self, fid: str):
        self.db.execute("DELETE FROM facts WHERE id=?", (fid,))
        self.db.execute("DELETE FROM facts_fts WHERE id=?", (fid,))

    def facts(self, limit=500) -> list[dict]:
        rows = self.db.all("SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            if r["ttl_days"] and r["created_at"] + r["ttl_days"] * 86400 < now_ts():
                continue
            out.append(r)
        return out

    def search_facts(self, query: str, limit=12) -> list[dict]:
        terms = [t for t in re.split(r"[\s,，。.!?？！;；:：]+", query or "") if len(t) >= 2][:12]
        rows: list[dict] = []
        if terms:
            q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                ids = [r["id"] for r in self.db.all("SELECT id FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?", (q, limit))]
            except Exception:
                ids = []
            if not ids:
                like = [f"%{t}%" for t in terms]
                cond = " OR ".join("fact LIKE ?" for _ in like)
                ids = [r["id"] for r in self.db.all(f"SELECT id FROM facts WHERE {cond} LIMIT ?", (*like, limit))]
            for i in ids:
                r = self.db.one("SELECT * FROM facts WHERE id=?", (i,))
                if r:
                    rows.append(r)
        return rows

    def add_episode(self, task_id: str, summary: str):
        self.db.insert("episodes", {"ts": now_ts(), "task_id": task_id, "summary": summary[:1000]})

    def episodes(self, limit=50) -> list[dict]:
        return self.db.all("SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ schedules
    def schedules(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM schedules ORDER BY created_at DESC")
        for r in rows:
            r["state"] = loads(r["state"], {})
        return rows

    def schedule(self, sid: str) -> dict | None:
        r = self.db.one("SELECT * FROM schedules WHERE id=?", (sid,))
        if r:
            r["state"] = loads(r["state"], {})
        return r

    # ------------------------------------------------------------ goals
    def goals(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM goals ORDER BY (status='active') DESC, created_at DESC")
        for r in rows:
            r["progress"] = loads(r["progress"], [])
        return rows

    def goal(self, gid: str) -> dict | None:
        r = self.db.one("SELECT * FROM goals WHERE id=?", (gid,))
        if r:
            r["progress"] = loads(r["progress"], [])
        return r

    # ------------------------------------------------------------ notifications
    def notify(self, title: str, body: str, task_id: str = "", level: str = "info") -> dict:
        nid = self.db.insert("notifications", {"ts": now_ts(), "title": title, "body": body, "task_id": task_id,
                                               "level": level, "read": 0})
        return {"id": nid, "ts": now_ts(), "title": title, "body": body, "task_id": task_id, "level": level, "read": 0}

    def notifications(self, limit=50) -> list[dict]:
        return self.db.all("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
