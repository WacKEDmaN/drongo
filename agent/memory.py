"""Persistent state: journal of activity, key/value memory, LLM usage meters.

Everything lives in a single SQLite file so the agent and the web dashboard
can share it without a server process.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT,            -- cycle | note | error | alert
    task_type TEXT,
    title     TEXT,
    body      TEXT,
    artifacts TEXT,            -- json list of {path,label}
    provider  TEXT,
    ok        INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT,
    ts    REAL
);
CREATE TABLE IF NOT EXISTS usage (
    provider     TEXT PRIMARY KEY,
    minute_key   INTEGER DEFAULT 0,
    minute_count INTEGER DEFAULT 0,
    day_key      TEXT DEFAULT '',
    day_count    INTEGER DEFAULT 0,
    cooldown_until REAL DEFAULT 0,
    total        INTEGER DEFAULT 0
);
"""


def utc_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


class Memory:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the agent (writer) and the dashboard (reader) share the file
        # in separate processes without blocking each other.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self._conn.executescript(SCHEMA)
        # Migration: add the tags column to journals created before tagging existed.
        try:
            self._conn.execute("ALTER TABLE journal ADD COLUMN tags TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass   # already there
        self._conn.commit()

    # ---- journal -------------------------------------------------------
    def add_journal(self, kind, title, body="", task_type="", artifacts=None,
                    provider="", ok=True) -> int:
        cur = self._conn.execute(
            "INSERT INTO journal (ts,kind,task_type,title,body,artifacts,provider,ok)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), kind, task_type, title, body,
             json.dumps(artifacts or []), provider, 1 if ok else 0),
        )
        self._conn.commit()
        return cur.lastrowid

    def recent_journal(self, limit=20):
        rows = self._conn.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_journal(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"]

    def count_projects(self) -> int:
        """How many projects it has concluded (finished or given up) all-time."""
        return self._conn.execute(
            "SELECT COUNT(*) AS c FROM journal WHERE kind='cycle'").fetchone()["c"]

    def reset_runtime(self, keep_keys=("settings",)) -> int:
        """Wipe ALL projects' history: clears the journal, the LLM usage/cooldown
        counters, and every kv entry EXCEPT keep_keys (settings are kept so API
        keys / persona / interests survive). Returns the number of journal rows
        removed. Caller is responsible for deleting the workspace files."""
        n = self.count_journal()
        self._conn.execute("DELETE FROM journal")
        self._conn.execute("DELETE FROM usage")
        if keep_keys:
            placeholders = ",".join("?" * len(keep_keys))
            self._conn.execute(f"DELETE FROM kv WHERE key NOT IN ({placeholders})",
                               tuple(keep_keys))
        else:
            self._conn.execute("DELETE FROM kv")
        self._conn.commit()
        return n

    def recent_task_titles(self, limit=12):
        rows = self._conn.execute(
            "SELECT title FROM journal WHERE kind='cycle' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["title"] for r in rows if r["title"]]

    def recent_projects(self, limit=12):
        """Recently FINISHED/attempted projects with type + tags — so ideation can
        steer away from repeats AND toward what the human rated highly."""
        rows = self._conn.execute(
            "SELECT title, task_type, tags FROM journal WHERE kind='cycle' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                tags = json.loads(r["tags"] or "[]")
            except Exception:
                tags = []
            out.append({"title": r["title"] or "", "task_type": r["task_type"] or "",
                        "tags": tags if isinstance(tags, list) else []})
        return out

    # ---- tags & the fix queue -----------------------------------------
    def set_tags(self, journal_id, tags):
        self._conn.execute("UPDATE journal SET tags=? WHERE id=?",
                           (json.dumps(tags), journal_id))
        self._conn.commit()

    def tag_entry(self, journal_id, tag, on=True):
        row = self._conn.execute("SELECT tags FROM journal WHERE id=?",
                                 (journal_id,)).fetchone()
        if not row:
            return []
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        if on and tag not in tags:
            tags.append(tag)
        if not on and tag in tags:
            tags.remove(tag)
        self.set_tags(journal_id, tags)
        return tags

    def delete_journal(self, journal_id) -> bool:
        cur = self._conn.execute("DELETE FROM journal WHERE id=?", (journal_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def set_suggestion(self, text):
        """A human steer for the NEXT new project (consumed once it's started)."""
        self.remember("suggestion", str(text).strip())

    def get_suggestion(self):
        return self.recall("suggestion") or ""

    def pop_suggestion(self):
        s = self.recall("suggestion") or ""
        if s:
            self.remember("suggestion", "")
        return s

    def add_lesson(self, text):
        """Record a one-line lesson learned from a project (capped ring of 25)."""
        text = str(text or "").strip()
        if not text:
            return
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        v.append({"t": time.time(), "txt": text[:200]})
        self.remember("lessons", v[-25:])

    def recent_lessons(self, limit=8):
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        return [x["txt"] for x in v[-limit:] if x.get("txt")]

    # ---- skills library + standing mission ----------------------------
    def add_skill(self, name, desc, code) -> bool:
        name = str(name or "").strip()[:60]
        if not name or not str(code or "").strip():
            return False
        v = [s for s in self.skills() if s.get("name") != name]   # replace same name
        v.append({"name": name, "desc": str(desc or "").strip()[:200],
                  "code": str(code)[:6000], "ts": time.time()})
        self.remember("skills", v[-30:])
        return True

    def skills(self) -> list:
        v = self.recall("skills")
        return v if isinstance(v, list) else []

    def get_skill(self, name):
        for s in self.skills():
            if s.get("name") == name:
                return s
        return None

    def add_note(self, topic, content) -> bool:
        content = str(content or "").strip()
        if not content:
            return False
        v = self.recall("notes")
        v = v if isinstance(v, list) else []
        v.append({"topic": str(topic or "").strip()[:80], "content": content[:2000],
                  "ts": time.time()})
        self.remember("notes", v[-50:])
        return True

    def search_notes(self, query, limit=5) -> list:
        q = str(query or "").lower().strip()
        v = self.recall("notes")
        v = v if isinstance(v, list) else []
        if not q:
            return v[-limit:]
        hits = [n for n in v if q in (n.get("topic", "") + " " + n.get("content", "")).lower()]
        return hits[-limit:]

    def set_mission(self, text):
        self.remember("mission", str(text or "").strip())

    def get_mission(self) -> str:
        return self.recall("mission") or ""

    def push_step(self, kind, text):
        """Append a short entry to the live-thinking ring buffer (last ~40) that
        the dashboard tails, so you can watch the agent work in real time."""
        v = self.recall("live_steps")
        v = v if isinstance(v, list) else []
        v.append({"k": kind, "txt": str(text)[:220], "ts": time.time()})
        self.remember("live_steps", v[-40:])

    def providers_off(self) -> list:
        """Provider names the human has manually switched off from the dashboard
        (kept across restarts; the Router skips them live, no restart needed)."""
        v = self.recall("providers_off")
        return v if isinstance(v, list) else []

    def set_provider_enabled(self, name, on) -> list:
        off = [x for x in self.providers_off() if x != name]
        if not on:
            off.append(name)
        self.remember("providers_off", off)
        return off

    def add_fix(self, entry: dict):
        q = self.recall("fix_queue") or []
        q.append(entry)
        self.remember("fix_queue", q)

    def remove_fix(self, journal_id) -> None:
        """Drop any queued fix that targets this journal entry (e.g. on delete)."""
        q = self.recall("fix_queue") or []
        kept = [e for e in q if e.get("id") != journal_id]
        if len(kept) != len(q):
            self.remember("fix_queue", kept)

    def pop_fix(self):
        q = self.recall("fix_queue") or []
        if not q:
            return None
        item = q.pop(0)
        self.remember("fix_queue", q)
        return item

    def fix_queue(self):
        return self.recall("fix_queue") or []

    # ---- key/value -----------------------------------------------------
    def remember(self, key, value):
        self._conn.execute(
            "INSERT INTO kv (key,value,ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    def recall(self, key, default=None):
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def all_kv(self):
        rows = self._conn.execute("SELECT key,value,ts FROM kv ORDER BY ts DESC").fetchall()
        out = []
        for r in rows:
            try:
                val = json.loads(r["value"])
            except Exception:
                val = r["value"]
            out.append({"key": r["key"], "value": val, "ts": r["ts"]})
        return out

    # ---- LLM usage metering -------------------------------------------
    def _usage_row(self, provider):
        row = self._conn.execute("SELECT * FROM usage WHERE provider=?", (provider,)).fetchone()
        if row is None:
            self._conn.execute("INSERT INTO usage (provider) VALUES (?)", (provider,))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM usage WHERE provider=?", (provider,)).fetchone()
        return dict(row)

    def can_use(self, provider, rpm_limit, daily_limit) -> bool:
        row = self._usage_row(provider)
        now = time.time()
        if row["cooldown_until"] and now < row["cooldown_until"]:
            return False
        minute_key = int(now // 60)
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        min_count = row["minute_count"] if row["minute_key"] == minute_key else 0
        day_count = row["day_count"] if row["day_key"] == day_key else 0
        if rpm_limit and min_count >= rpm_limit:
            return False
        if daily_limit and day_count >= daily_limit:
            return False
        return True

    def record_use(self, provider):
        row = self._usage_row(provider)
        now = time.time()
        minute_key = int(now // 60)
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        min_count = (row["minute_count"] if row["minute_key"] == minute_key else 0) + 1
        day_count = (row["day_count"] if row["day_key"] == day_key else 0) + 1
        self._conn.execute(
            "UPDATE usage SET minute_key=?,minute_count=?,day_key=?,day_count=?,"
            "total=total+1 WHERE provider=?",
            (minute_key, min_count, day_key, day_count, provider),
        )
        self._conn.commit()

    def set_cooldown(self, provider, seconds):
        self._usage_row(provider)
        self._conn.execute(
            "UPDATE usage SET cooldown_until=? WHERE provider=?",
            (time.time() + seconds, provider),
        )
        self._conn.commit()

    def clear_cooldowns(self):
        """Drop all provider cooldowns — called at startup so a restart (e.g.
        after fixing a model id) immediately re-tries every provider."""
        self._conn.execute("UPDATE usage SET cooldown_until=0")
        self._conn.commit()

    def usage_summary(self):
        rows = self._conn.execute("SELECT * FROM usage ORDER BY provider").fetchall()
        return [dict(r) for r in rows]

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
