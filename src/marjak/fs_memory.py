# fs_memory.py — Mārjak v2: Persistent SQLite Filesystem Memory
#
# Directory-level only. Files are ephemeral (caches rewrite constantly),
# directories are structural (stable for weeks). Session playbook handles
# file-level FIDs during conversations.
#
# Storage: ~/.marjak/fs_memory.db
# Caps: 500 directory rows, 80 search_hits, 100 action_log entries

import json
import os
import re
import sqlite3
import subprocess
import shutil
from datetime import datetime, timedelta


_DB_DIR = os.path.expanduser("~/.marjak")
_DB_PATH = os.path.join(_DB_DIR, "fs_memory.db")


def _human_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{round(size_bytes / 1024 ** 3, 2)} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{round(size_bytes / 1024 ** 2, 1)} MB"
    elif size_bytes >= 1024:
        return f"{round(size_bytes / 1024, 1)} KB"
    return f"{size_bytes} B"


class FSMemory:
    """Persistent directory-level filesystem knowledge stored in SQLite.

    Two storage tiers:
    - Skeleton rows (depth ≤ 2 from ~): populated on first run, never evicted
    - Explored rows (depth > 2): LRU eviction when over 500 rows
    """

    MAX_DIRECTORIES = 500
    MAX_SEARCH_HITS = 80
    MAX_ACTIONS = 100

    def __init__(self, db_path: str = _DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS directories (
                path TEXT PRIMARY KEY,
                parent_path TEXT,
                name TEXT NOT NULL,
                depth INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                item_count INTEGER DEFAULT 0,
                top_children TEXT,
                scanned_at TEXT NOT NULL,
                is_skeleton INTEGER DEFAULT 0,
                times_visited INTEGER DEFAULT 1,
                description TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dir_parent ON directories(parent_path);
            CREATE INDEX IF NOT EXISTS idx_dir_size ON directories(size_bytes DESC);

            CREATE TABLE IF NOT EXISTS search_hits (
                path TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                size_bytes INTEGER DEFAULT 0,
                discovered_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS actions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                path TEXT,
                detail TEXT,
                timestamp TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) FROM directories").fetchone()
        return row[0] == 0

    def upsert_directory(
        self,
        path: str,
        name: str,
        size_bytes: int,
        item_count: int = 0,
        top_children: list | None = None,
        description: str = "",
        is_skeleton: bool = False,
    ):
        """Insert or update a directory row. Computes parent_path and depth automatically."""
        home = os.path.expanduser("~")
        expanded = os.path.expanduser(path).rstrip("/")
        if not expanded:
            expanded = "/"

        parent = os.path.dirname(expanded)
        # Depth relative to home
        try:
            rel = os.path.relpath(expanded, home)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
        except ValueError:
            depth = expanded.count("/")

        children_json = json.dumps(top_children) if top_children else None
        now = datetime.now().isoformat()

        self._conn.execute(
            """INSERT INTO directories
               (path, parent_path, name, depth, size_bytes, item_count,
                top_children, scanned_at, is_skeleton, times_visited, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(path) DO UPDATE SET
                 size_bytes = excluded.size_bytes,
                 item_count = excluded.item_count,
                 top_children = excluded.top_children,
                 scanned_at = excluded.scanned_at,
                 is_skeleton = CASE WHEN excluded.is_skeleton = 0 THEN 0 ELSE is_skeleton END,
                 times_visited = times_visited + 1,
                 description = CASE WHEN excluded.description != ''
                               THEN excluded.description ELSE description END
            """,
            (expanded, parent, name, depth, size_bytes, item_count,
             children_json, now, int(is_skeleton), description),
        )
        self._conn.commit()
        self._evict_if_needed()

    def upsert_search_hit(self, path: str, query: str, size_bytes: int = 0):
        now = datetime.now().isoformat()
        self._conn.execute(
            """INSERT INTO search_hits (path, query, size_bytes, discovered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 query = excluded.query,
                 size_bytes = excluded.size_bytes,
                 discovered_at = excluded.discovered_at
            """,
            (path, query, size_bytes, now),
        )
        self._conn.commit()
        # Enforce cap
        count = self._conn.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0]
        if count > self.MAX_SEARCH_HITS:
            self._conn.execute(
                """DELETE FROM search_hits WHERE path IN (
                     SELECT path FROM search_hits
                     ORDER BY discovered_at ASC
                     LIMIT ?
                   )""",
                (count - self.MAX_SEARCH_HITS,),
            )
            self._conn.commit()

    def log_action(self, action: str, path: str = "", detail: str = ""):
        now = datetime.now().isoformat()
        self._conn.execute(
            "INSERT INTO actions_log (action, path, detail, timestamp) VALUES (?, ?, ?, ?)",
            (action, path, detail[:500], now),
        )
        self._conn.commit()
        # Enforce circular cap
        count = self._conn.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0]
        if count > self.MAX_ACTIONS:
            self._conn.execute(
                """DELETE FROM actions_log WHERE id IN (
                     SELECT id FROM actions_log
                     ORDER BY id ASC LIMIT ?
                   )""",
                (count - self.MAX_ACTIONS,),
            )
            self._conn.commit()

    def delete_path(self, path: str):
        """Delete a path and all its children from directories."""
        expanded = os.path.expanduser(path).rstrip("/")
        self._conn.execute(
            "DELETE FROM directories WHERE path = ? OR path LIKE ?",
            (expanded, expanded + "/%"),
        )
        self._conn.execute("DELETE FROM search_hits WHERE path = ? OR path LIKE ?",
                           (expanded, expanded + "/%"))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_hotspots(self, n: int = 5) -> list[dict]:
        rows = self._conn.execute(
            """SELECT path, name, size_bytes, scanned_at, times_visited
               FROM directories
               WHERE is_skeleton = 0
               ORDER BY size_bytes DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_skeleton(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT path, name, size_bytes, item_count, top_children, scanned_at
               FROM directories
               WHERE is_skeleton = 1
               ORDER BY size_bytes DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_children(self, parent_path: str) -> list[dict]:
        expanded = os.path.expanduser(parent_path).rstrip("/")
        rows = self._conn.execute(
            "SELECT * FROM directories WHERE parent_path = ? ORDER BY size_bytes DESC",
            (expanded,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stale_paths(self, max_age_days: int = 7) -> list[dict]:
        threshold = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        rows = self._conn.execute(
            """SELECT path, name, size_bytes, scanned_at
               FROM directories
               WHERE scanned_at < ? AND is_skeleton = 0
               ORDER BY scanned_at ASC
               LIMIT 10""",
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_actions(self, n: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT action, path, detail, timestamp FROM actions_log ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_directory(self, path: str) -> dict | None:
        """Get a single directory row by path."""
        expanded = os.path.expanduser(path).rstrip("/")
        row = self._conn.execute(
            "SELECT * FROM directories WHERE path = ?", (expanded,)
        ).fetchone()
        return dict(row) if row else None

    def get_context_for_query(self, query: str) -> str:
        """Build a compact context string for the LLM system prompt.

        Returns relevant persistent knowledge based on keyword matching:
        - Hotspots (top 5 by size)
        - Recent actions (last 5)
        - Stale paths (>7 days old)
        - Keyword-matched directories
        """
        parts = []

        # 1. Skeleton summary (always — gives the model system awareness)
        skeleton = self.get_skeleton()
        if skeleton:
            home = os.path.expanduser("~")
            items = []
            for s in skeleton[:20]:
                try:
                    rel = "~/" + os.path.relpath(s["path"], home)
                except (ValueError, TypeError):
                    rel = s["path"]
                age = self._age_str(s["scanned_at"])
                items.append(f"{rel} ({_human_size(s['size_bytes'])}, {age})")
            parts.append("SYSTEM MAP: " + ", ".join(items))

        # 2. Hotspots
        hotspots = self.get_hotspots(10)
        if hotspots:
            home = os.path.expanduser("~")
            items = []
            for h in hotspots:
                try:
                    rel = "~/" + os.path.relpath(h["path"], home)
                except (ValueError, TypeError):
                    rel = h["path"]
                age = self._age_str(h["scanned_at"])
                items.append(f"{rel} ({_human_size(h['size_bytes'])}, {age})")
            parts.append("HOTSPOTS: " + ", ".join(items))

        # 3. Keyword-matched directories (if query has specific terms)
        if query:
            keywords = self._extract_keywords(query)
            if keywords:
                matched = self._keyword_search(keywords, limit=10)
                if matched:
                    home = os.path.expanduser("~")
                    items = []
                    for m in matched:
                        try:
                            rel = "~/" + os.path.relpath(m["path"], home)
                        except (ValueError, TypeError):
                            rel = m["path"]
                        items.append(f"{rel} ({_human_size(m['size_bytes'])})")
                    parts.append("RELEVANT: " + ", ".join(items))

        # 4. Stale paths
        stale = self.get_stale_paths()
        if stale:
            home = os.path.expanduser("~")
            items = []
            for s in stale[:3]:
                try:
                    rel = "~/" + os.path.relpath(s["path"], home)
                except (ValueError, TypeError):
                    rel = s["path"]
                age = self._age_str(s["scanned_at"])
                items.append(f"{rel} ({age})")
            parts.append("STALE (re-navigate to confirm): " + ", ".join(items))

        # 5. Recent actions
        actions = self.get_recent_actions(10)
        if actions:
            items = []
            for a in actions:
                ts = a["timestamp"][:10] if a["timestamp"] else "?"
                items.append(f"[{ts}] {a['action']}: {a['detail'][:60]}")
            parts.append("RECENT: " + "; ".join(items))

        return "\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # First-run scan
    # ------------------------------------------------------------------

    def run_first_scan(self):
        """Silent 2-level scan of ~ and ~/Library. ~40-55 rows, ~3-5 sec."""
        mo = shutil.which("mo") or "/opt/homebrew/bin/mo"
        if not os.path.isfile(mo):
            import logging
            logging.getLogger(__name__).warning("mo binary not found — first scan skipped. Install: brew install mo")
            return
        home = os.path.expanduser("~")
        targets = [home, os.path.join(home, "Library")]

        for target in targets:
            try:
                result = subprocess.run(
                    [mo, "analyze", "--json", target],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0:
                    continue
                data = json.loads(result.stdout)
                # Store the target directory itself
                total_size = data.get("total_size", 0)
                entries = data.get("entries", [])
                name = os.path.basename(target) or "~"
                top_children = [
                    {"name": e["name"], "size": e.get("size", 0), "is_dir": e.get("is_dir", False)}
                    for e in sorted(entries, key=lambda x: x.get("size", 0), reverse=True)[:10]
                ]
                self.upsert_directory(
                    path=target, name=name, size_bytes=total_size,
                    item_count=len(entries), top_children=top_children,
                    is_skeleton=True,
                )
                # Store each child directory
                for e in entries:
                    if e.get("is_dir"):
                        self.upsert_directory(
                            path=e["path"], name=e["name"],
                            size_bytes=e.get("size", 0),
                            item_count=e.get("item_count", 0),
                            is_skeleton=True,
                        )
            except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
                continue

    # ------------------------------------------------------------------
    # Eviction & maintenance
    # ------------------------------------------------------------------

    def _evict_if_needed(self):
        """LRU eviction of non-skeleton directory rows when over cap."""
        count = self._conn.execute(
            "SELECT COUNT(*) FROM directories WHERE is_skeleton = 0"
        ).fetchone()[0]
        if count <= self.MAX_DIRECTORIES:
            return
        excess = count - self.MAX_DIRECTORIES
        self._conn.execute(
            """DELETE FROM directories WHERE path IN (
                 SELECT path FROM directories
                 WHERE is_skeleton = 0
                 ORDER BY scanned_at ASC
                 LIMIT ?
               )""",
            (excess,),
        )
        self._conn.commit()

    def wipe(self):
        """Drop all tables and recreate — full reset."""
        self._conn.executescript("""
            DROP TABLE IF EXISTS directories;
            DROP TABLE IF EXISTS search_hits;
            DROP TABLE IF EXISTS actions_log;
        """)
        self._create_tables()

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _age_str(iso_timestamp: str) -> str:
        """Convert ISO timestamp to human-readable age like '3d ago'."""
        try:
            then = datetime.fromisoformat(iso_timestamp)
            delta = datetime.now() - then
            if delta.days > 0:
                return f"{delta.days}d ago"
            hours = delta.seconds // 3600
            if hours > 0:
                return f"{hours}h ago"
            return "just now"
        except (ValueError, TypeError):
            return "unknown"

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        """Extract meaningful keywords from a user query."""
        stop = {"the", "a", "an", "is", "in", "on", "my", "me", "how", "what",
                "much", "space", "does", "do", "can", "i", "to", "of", "and",
                "for", "it", "this", "that", "with", "from", "up", "about"}
        words = re.findall(r"[a-zA-Z0-9_./-]+", query.lower())
        return [w for w in words if w not in stop and len(w) > 2]

    def _keyword_search(self, keywords: list[str], limit: int = 5) -> list[dict]:
        """Search directories by keyword match on path/name/description."""
        if not keywords:
            return []
        conditions = []
        params = []
        for kw in keywords[:5]:  # Cap to avoid huge queries
            like = f"%{kw}%"
            conditions.append("(path LIKE ? OR name LIKE ? OR description LIKE ?)")
            params.extend([like, like, like])

        where = " OR ".join(conditions)
        rows = self._conn.execute(
            f"""SELECT path, name, size_bytes, scanned_at
                FROM directories
                WHERE {where}
                ORDER BY size_bytes DESC
                LIMIT ?""",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
fs_memory = FSMemory()
