"""Persistent state: journal of activity, key/value memory, LLM usage meters.

Everything lives in a single SQLite file so the agent and the web dashboard
can share it without a server process.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def _ts_eq(a, b) -> bool:
    """Match two timestamps across a JSON round-trip (the dashboard sends a note's
    ts back as the id to edit/delete). Small epsilon guards float formatting."""
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


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
    total        INTEGER DEFAULT 0,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    day_tokens   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_daily (
    provider   TEXT NOT NULL,
    day        TEXT NOT NULL,
    calls      INTEGER DEFAULT 0,
    tokens_in  INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    provider   TEXT,
    model      TEXT,
    purpose    TEXT,            -- ideate | plan | execute | critique | chat | …
    tokens_in  INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    ms         INTEGER DEFAULT 0,
    status     TEXT DEFAULT 'ok'
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
            # Bound the WAL. The dashboard is a constant reader, which can hold back
            # checkpoints; without a size limit the -wal file grows to its high-water
            # mark and stays there, and on a small SD card that trends toward a full
            # disk (→ box wedges). Cap it and let it truncate back down on checkpoint.
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.execute("PRAGMA journal_size_limit=%d" % (32 * 1024 * 1024))
        except Exception:
            pass
        self._conn.executescript(SCHEMA)
        # Migration: token columns on the usage table (DBs created before token metering).
        for col in ("tokens_in INTEGER DEFAULT 0", "tokens_out INTEGER DEFAULT 0",
                    "day_tokens INTEGER DEFAULT 0"):
            try:
                self._conn.execute(f"ALTER TABLE usage ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
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

    def recent_journal(self, limit=20, kind=None):
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM journal WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)
            ).fetchall()
        else:
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

    def add_lesson(self, text) -> bool:
        """Record a one-line lesson learned from a project (capped ring of 25)."""
        text = str(text or "").strip()
        if not text:
            return False
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        v.append({"t": time.time(), "txt": text[:200]})
        self.remember("lessons", v[-25:])
        return True

    def recent_lessons(self, limit=8):
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        return [x["txt"] for x in v[-limit:] if x.get("txt")]

    def lessons_full(self, limit=25) -> list:
        """Lessons WITH their timestamps — the id the dashboard edits/deletes by."""
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        return v[-limit:]

    def delete_lesson(self, t) -> bool:
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        nv = [x for x in v if not _ts_eq(x.get("t"), t)]
        if len(nv) == len(v):
            return False
        self.remember("lessons", nv)
        return True

    def edit_lesson(self, t, text) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        v = self.recall("lessons")
        v = v if isinstance(v, list) else []
        hit = False
        for x in v:
            if _ts_eq(x.get("t"), t):
                x["txt"] = text[:200]
                hit = True
        if hit:
            self.remember("lessons", v)
        return hit

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

    def delete_skill(self, name) -> bool:
        v = self.skills()
        nv = [s for s in v if s.get("name") != name]
        if len(nv) == len(v):
            return False
        self.remember("skills", nv)
        return True

    def notes(self) -> list:
        v = self.recall("notes")
        return v if isinstance(v, list) else []

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

    def delete_note(self, ts) -> bool:
        v = self.recall("notes")
        v = v if isinstance(v, list) else []
        nv = [n for n in v if not _ts_eq(n.get("ts"), ts)]
        if len(nv) == len(v):
            return False
        self.remember("notes", nv)
        return True

    def edit_note(self, ts, topic, content) -> bool:
        content = str(content or "").strip()
        if not content:
            return False
        v = self.recall("notes")
        v = v if isinstance(v, list) else []
        hit = False
        for n in v:
            if _ts_eq(n.get("ts"), ts):
                n["topic"] = str(topic or "").strip()[:80]
                n["content"] = content[:2000]
                hit = True
        if hit:
            self.remember("notes", v)
        return hit

    @staticmethod
    def _tokens(text) -> set:
        return set(re.findall(r"[a-z0-9]{3,}", str(text or "").lower()))

    def relevant_knowledge(self, query, k=5) -> list:
        """Lightweight RAG: rank everything the agent knows — its own SKILLS,
        NOTES, LESSONS, indexed REPO files (its own code/docs) and past PROJECTS —
        by word-overlap with `query`, and return the top-k most relevant, each
        {kind, title, text}. Pure stdlib (no embeddings/vectors) so it stays light
        on the Pi; good enough to surface 'have I done/seen this before?' into a
        build and to make the repo the agent's first-class context."""
        q = self._tokens(query)
        if not q:
            return []
        scored = []
        for s in self.skills():
            toks = self._tokens(f"{s.get('name','')} {s.get('desc','')}")
            scored.append((len(q & toks), {"kind": "skill", "title": s.get("name", ""),
                                           "text": s.get("desc", "")}))
        for n in (self.recall("notes") or []):
            if not isinstance(n, dict):
                continue
            toks = self._tokens(f"{n.get('topic','')} {n.get('content','')}")
            scored.append((len(q & toks), {"kind": "note", "title": n.get("topic", ""),
                                           "text": (n.get("content", "") or "")[:280]}))
        for l in (self.recall("lessons") or []):
            if not isinstance(l, dict):
                continue
            scored.append((len(q & self._tokens(l.get("txt", ""))),
                           {"kind": "lesson", "title": "", "text": l.get("txt", "")}))
        for r in (self.recall("repo_index") or []):
            if not isinstance(r, dict):
                continue
            toks = self._tokens(f"{r.get('path','')} {r.get('summary','')}")
            scored.append((len(q & toks), {"kind": "repo", "title": r.get("path", ""),
                                           "text": (r.get("summary", "") or "")[:280]}))
        for j in self.recent_journal(60, kind="cycle"):
            toks = self._tokens(f"{j.get('title','')} {j.get('body','')}")
            scored.append((len(q & toks), {"kind": "project", "title": j.get("title", ""),
                                           "text": (j.get("body", "") or "")[:200]}))
        scored = [x for x in scored if x[0] > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:k]]

    def index_repo(self, repo_dir, patterns=("agent/*.py", "system/*.py", "*.md",
                                             "config.example.yaml")) -> int:
        """Index the agent's OWN codebase + docs into the knowledge base, so
        relevant_knowledge / recall_knowledge can surface 'how does my own X work?'
        — the repo becomes first-class context. Cheap; called once at startup (and
        after a self-update). Reads only (the code dir is read-only to the agent)."""
        entries = []
        base = str(repo_dir)
        for pat in patterns:
            for path in sorted(glob.glob(os.path.join(base, pat))):
                rel = os.path.relpath(path, base).replace(os.sep, "/")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(20000)
                except Exception:
                    continue
                summ = self._summarize_source(rel, text)
                if summ:
                    entries.append({"path": rel, "summary": summ[:400]})
        self.remember("repo_index", entries)
        return len(entries)

    @staticmethod
    def _summarize_source(rel, text) -> str:
        low = rel.lower()
        if low.endswith(".py"):
            m = re.search(r'"""(.+?)"""', text, re.S)
            doc = (m.group(1).strip().splitlines()[0] if m else "")[:160]
            defs = re.findall(r'^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)', text, re.M)
            names = ", ".join(dict.fromkeys(defs))[:200]
            joiner = " — " if doc and names else ""
            return (doc + joiner + (("defines: " + names) if names else "")).strip()
        if low.endswith((".md", ".yaml", ".yml")):
            lines = [ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()]
            return " · ".join(lines[:3])[:300]
        return text.strip()[:200]

    def request_package(self, name, reason) -> bool:
        name = str(name or "").strip()[:60]
        if not name:
            return False
        v = self.pkg_requests()
        if any(p.get("name") == name for p in v):
            return True                              # already requested
        v.append({"name": name, "reason": str(reason or "").strip()[:200], "ts": time.time()})
        self.remember("pkg_requests", v[-40:])
        return True

    def pkg_requests(self) -> list:
        v = self.recall("pkg_requests")
        return v if isinstance(v, list) else []

    def resolve_package(self, name, installed=True):
        """Drop a package request. If installed=True, record it as now-available
        so the agent is told it can use it."""
        self.remember("pkg_requests", [p for p in self.pkg_requests() if p.get("name") != name])
        if installed:
            e = self.installed_extras()
            if name not in e:
                e.append(name)
                self.remember("installed_extras", e[-80:])

    def installed_extras(self) -> list:
        e = self.recall("installed_extras")
        return e if isinstance(e, list) else []

    # ---- scoped package-install policy (read by the root pkg-installer) ----
    def pkg_policy(self) -> dict:
        p = self.recall("pkg_policy")
        if not isinstance(p, dict):
            p = {}
        return {"mode": "auto" if p.get("mode") == "auto" else "manual",
                "allow": [str(x) for x in (p.get("allow") or []) if str(x).strip()]}

    def set_pkg_policy(self, mode=None, allow=None):
        p = self.pkg_policy()
        if mode in ("auto", "manual"):
            p["mode"] = mode
        if isinstance(allow, list):
            # de-dup, keep only sane glob/name entries
            seen, clean = set(), []
            for a in allow:
                a = str(a).strip()[:60]
                if a and a not in seen and re.match(r"^[a-z0-9][a-z0-9+.*?-]*$", a):
                    seen.add(a); clean.append(a)
            p["allow"] = clean
        self.remember("pkg_policy", p)
        return p

    def sync_installed_markers(self, workspace) -> list:
        """Consume the root installer's done-markers: mark each requested package
        resolved (installed or not) and delete the marker. Returns newly-installed
        names. Called by the agent each cycle so its own DB stays the source of
        truth (the root helper never writes the DB)."""
        d = Path(workspace) / ".pkg-installed"
        if not d.is_dir():
            return []
        done = []
        for f in d.iterdir():
            if not f.is_file():
                continue
            try:
                ok = bool(json.loads(f.read_text()).get("ok"))
            except Exception:
                ok = True
            self.resolve_package(f.name, installed=ok)
            if ok:
                done.append(f.name)
            try:
                f.unlink()
            except OSError:
                pass
        return done

    def set_mission(self, text):
        self.remember("mission", str(text or "").strip())

    def get_mission(self) -> str:
        return self.recall("mission") or ""

    def push_step(self, kind, text):
        """Append a short entry to the live-thinking ring buffer (last ~40) that
        the dashboard tails, so you can watch the agent work in real time."""
        v = self.recall("live_steps")
        v = v if isinstance(v, list) else []
        v.append({"k": kind, "txt": str(text)[:2000], "ts": time.time()})
        self.remember("live_steps", v[-80:])

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

    def journal_has(self, journal_id) -> bool:
        """True if a journal entry with this id still exists (False once deleted)."""
        try:
            row = self._conn.execute("SELECT 1 FROM journal WHERE id=?",
                                     (int(journal_id),)).fetchone()
        except (TypeError, ValueError):
            return False
        return row is not None

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

    def forget(self, key) -> bool:
        """Remove a key from the kv store entirely (the dashboard memory browser)."""
        cur = self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

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

    def record_use(self, provider, tokens_in=0, tokens_out=0):
        row = self._usage_row(provider)
        now = time.time()
        tokens_in, tokens_out = int(tokens_in or 0), int(tokens_out or 0)
        minute_key = int(now // 60)
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        same_day = row["day_key"] == day_key
        min_count = (row["minute_count"] if row["minute_key"] == minute_key else 0) + 1
        day_count = (row["day_count"] if same_day else 0) + 1
        day_tok = (row.get("day_tokens", 0) if same_day else 0) + tokens_in + tokens_out
        self._conn.execute(
            "UPDATE usage SET minute_key=?,minute_count=?,day_key=?,day_count=?,"
            "total=total+1,tokens_in=tokens_in+?,tokens_out=tokens_out+?,day_tokens=? "
            "WHERE provider=?",
            (minute_key, min_count, day_key, day_count, tokens_in, tokens_out, day_tok, provider),
        )
        # Per-day time series for the graphs (upsert).
        self._conn.execute(
            "INSERT INTO usage_daily (provider,day,calls,tokens_in,tokens_out) VALUES (?,?,1,?,?) "
            "ON CONFLICT(provider,day) DO UPDATE SET calls=calls+1,"
            "tokens_in=tokens_in+excluded.tokens_in,tokens_out=tokens_out+excluded.tokens_out",
            (provider, day_key, tokens_in, tokens_out),
        )
        self._conn.commit()

    def usage_daily_series(self, days=14):
        """Recent per-provider daily usage for the dashboard graphs."""
        rows = self._conn.execute(
            "SELECT provider,day,calls,tokens_in,tokens_out FROM usage_daily "
            "ORDER BY day DESC LIMIT ?", (days * 12,)).fetchall()
        return [dict(r) for r in rows]

    # ---- per-request log (dashboard "Requests" view) ------------------
    def record_request(self, provider, model="", purpose="", tokens_in=0,
                        tokens_out=0, ms=0, status="ok"):
        """Log ONE LLM call — tokens metered per request. Kept to the last ~500
        rows so the table can't grow without bound on a long-running box."""
        self._conn.execute(
            "INSERT INTO requests (ts,provider,model,purpose,tokens_in,tokens_out,ms,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), provider, model, purpose, int(tokens_in or 0),
             int(tokens_out or 0), int(ms or 0), status))
        self._conn.execute(
            "DELETE FROM requests WHERE id <= (SELECT MAX(id)-500 FROM requests)")
        self._conn.commit()

    def recent_requests(self, limit=120):
        rows = self._conn.execute(
            "SELECT ts,provider,model,purpose,tokens_in,tokens_out,ms,status "
            "FROM requests ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def requests_summary(self):
        """Totals for the Requests view header (today + all-time in the ring)."""
        row = self._conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(tokens_in),0) ti, "
            "COALESCE(SUM(tokens_out),0) to_ FROM requests").fetchone()
        return {"count": row["c"], "tokens_in": row["ti"], "tokens_out": row["to_"]}

    # ---- chat with the human (dashboard Chat tab) ---------------------
    def add_chat(self, role, content, provider="", tin=0, tout=0):
        v = self.recall("chat")
        v = v if isinstance(v, list) else []
        m = {"role": role, "content": str(content or "")[:4000], "ts": time.time()}
        if provider:
            m.update({"provider": provider, "tin": int(tin or 0), "tout": int(tout or 0)})
        v.append(m)
        self.remember("chat", v[-60:])

    def chat_history(self, limit=40):
        v = self.recall("chat")
        v = v if isinstance(v, list) else []
        return v[-limit:]

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
