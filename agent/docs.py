"""Reference documents you upload → a lightweight retrieval index: DRONGO's RAG
"source of truth". Text is split into passages and indexed with SQLite **FTS5**
(BM25 ranking, zero extra dependencies). If this SQLite build lacks FTS5 we fall
back to a plain table + token-overlap scoring. No embeddings / no torch, so it
stays light on the Pi — the agent searches these docs and treats them as
authoritative when building.
"""

from __future__ import annotations

import html as _html
import re
import sqlite3
import time
from pathlib import Path

# Text-ish files we can index. Binary formats (pdf/docx) need a parser — skipped
# for now (the uploader reports them as unsupported).
TEXT_EXTS = (".txt", ".md", ".markdown", ".rst", ".text", ".log", ".csv", ".tsv",
             ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
             ".py", ".js", ".ts", ".jsx", ".tsx", ".c", ".h", ".cpp", ".hpp", ".cc",
             ".java", ".go", ".rs", ".rb", ".php", ".lua", ".sh", ".bash",
             ".html", ".htm", ".xml", ".css", ".sql", ".asm", ".s")


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style|head|nav|footer).*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return _html.unescape(re.sub(r"[ \t]+", " ", t))


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list:
    """Split into overlapping passages, preferring newline breaks near the end."""
    text = (text or "").strip()
    if not text:
        return []
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(i + size, n)
        if j < n:
            k = text.rfind("\n", i + size // 2, j)
            if k != -1:
                j = k
        piece = text[i:j].strip()
        if piece:
            out.append(piece)
        i = (j - overlap) if (j - overlap) > i else j
    return out


class DocStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self.fts = True
        try:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs "
                             "USING fts5(source, title, body, tokenize='porter unicode61')")
        except Exception:
            self.fts = False
            self.con.execute("CREATE TABLE IF NOT EXISTS docs "
                             "(source TEXT, title TEXT, body TEXT)")
        self.con.commit()

    # ---- ingest --------------------------------------------------------
    def add_text(self, source: str, title: str, text: str) -> int:
        source = str(source)[:400]
        title = str(title or source)[:200]
        self.delete(source)                      # replace-by-source (re-upload updates)
        n = 0
        for ch in chunk_text(text):
            self.con.execute("INSERT INTO docs (source,title,body) VALUES (?,?,?)",
                             (source, title, ch))
            n += 1
        self.con.commit()
        return n

    def add_file(self, path, source: str | None = None) -> int:
        p = Path(path)
        if p.suffix.lower() not in TEXT_EXTS:
            return 0
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return 0
        if p.suffix.lower() in (".html", ".htm", ".xml"):
            raw = _strip_html(raw)
        return self.add_text(source or p.name, p.name, raw)

    def index_dir(self, folder) -> int:
        """(Re)index every text file under a folder. Returns chunks indexed."""
        folder = Path(folder)
        total = 0
        if not folder.is_dir():
            return 0
        for f in sorted(folder.rglob("*")):
            if f.is_file() and f.suffix.lower() in TEXT_EXTS:
                rel = str(f.relative_to(folder)).replace("\\", "/")
                total += self.add_file(f, source=rel)
        return total

    # ---- retrieval -----------------------------------------------------
    def search(self, query: str, k: int = 6) -> list:
        q = (query or "").strip()
        if not q:
            return []
        if self.fts:
            terms = re.findall(r"[A-Za-z0-9_]{2,}", q)
            if terms:
                match = " OR ".join(terms[:12])
                try:
                    rows = self.con.execute(
                        "SELECT source,title,body,bm25(docs) AS score FROM docs "
                        "WHERE docs MATCH ? ORDER BY score LIMIT ?", (match, k)).fetchall()
                    if rows:
                        return [{"source": r["source"], "title": r["title"],
                                 "snippet": r["body"][:600]} for r in rows]
                except Exception:
                    pass
        qt = set(re.findall(r"[a-z0-9]{3,}", q.lower()))
        scored = []
        for r in self.con.execute("SELECT source,title,body FROM docs").fetchall():
            s = len(qt & set(re.findall(r"[a-z0-9]{3,}", (r["body"] or "").lower())))
            if s:
                scored.append((s, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"source": r["source"], "title": r["title"], "snippet": r["body"][:600]}
                for _, r in scored[:k]]

    # ---- manage --------------------------------------------------------
    def documents(self) -> list:
        rows = self.con.execute(
            "SELECT source, MIN(title) AS title, COUNT(*) AS chunks, "
            "SUM(length(body)) AS bytes FROM docs GROUP BY source ORDER BY source").fetchall()
        return [dict(r) for r in rows]

    def delete(self, source: str) -> int:
        cur = self.con.execute("DELETE FROM docs WHERE source=?", (str(source),))
        self.con.commit()
        return cur.rowcount

    def clear(self) -> None:
        self.con.execute("DELETE FROM docs")
        self.con.commit()

    def count(self) -> dict:
        r = self.con.execute("SELECT COUNT(DISTINCT source) AS d, COUNT(*) AS c FROM docs").fetchone()
        return {"documents": r["d"] or 0, "chunks": r["c"] or 0}

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
