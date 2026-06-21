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

    def recent_task_titles(self, limit=12):
        rows = self._conn.execute(
            "SELECT title FROM journal WHERE kind='cycle' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["title"] for r in rows if r["title"]]

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
