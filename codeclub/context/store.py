"""
SQLite-backed session store for dynamic context.

Stores conversation sessions as episodes (groups of related turns),
with code references, decisions, and artifacts.  FTS5 for full-text search.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path("context_session.db")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id          TEXT PRIMARY KEY,
            topic       TEXT NOT NULL,
            intent      TEXT NOT NULL,
            summary     TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            closed_at   REAL
        );

        CREATE TABLE IF NOT EXISTS turns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id  TEXT NOT NULL REFERENCES episodes(id),
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            intent      TEXT,
            tokens      INTEGER,
            created_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_refs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id  TEXT NOT NULL REFERENCES episodes(id),
            turn_id     INTEGER REFERENCES turns(id),
            file_path   TEXT NOT NULL,
            symbols     TEXT,
            ref_type    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id  TEXT NOT NULL REFERENCES episodes(id),
            turn_id     INTEGER REFERENCES turns(id),
            decision    TEXT NOT NULL,
            rationale   TEXT,
            supersedes  INTEGER REFERENCES decisions(id)
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id      TEXT NOT NULL REFERENCES episodes(id),
            turn_id         INTEGER REFERENCES turns(id),
            artifact_type   TEXT NOT NULL,
            name            TEXT,
            content         TEXT NOT NULL,
            tokens          INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_episode ON turns(episode_id);
        CREATE INDEX IF NOT EXISTS idx_code_refs_episode ON code_refs(episode_id);
        CREATE INDEX IF NOT EXISTS idx_decisions_episode ON decisions(episode_id);
        CREATE INDEX IF NOT EXISTS idx_artifacts_episode ON artifacts(episode_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
            content,
            source_type,
            source_id UNINDEXED
        );
    """)


class SessionStore:
    """SQLite-backed session store for dynamic context."""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.conn = _connect(self.db_path)

    # ── Episodes ─────────────────────────────────────────────────────

    def create_episode(self, topic: str, intent: str) -> str:
        """Create a new episode and return its id."""
        eid = uuid.uuid4().hex
        now = time.time()
        self.conn.execute(
            "INSERT INTO episodes (id, topic, intent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, topic, intent, now, now),
        )
        self.conn.commit()
        return eid

    def close_episode(self, episode_id: str, summary: str | None = None) -> None:
        """Close an episode, optionally storing a summary."""
        now = time.time()
        self.conn.execute(
            "UPDATE episodes SET closed_at = ?, updated_at = ?, summary = COALESCE(?, summary) "
            "WHERE id = ?",
            (now, now, summary, episode_id),
        )
        self.conn.commit()

    def active_episode(self) -> dict | None:
        """Return the most recent unclosed episode, or None."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None

    def get_episode(self, episode_id: str) -> dict | None:
        """Return a single episode by id."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_episodes(self, limit: int = 50) -> list[dict]:
        """Return recent episodes, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM episodes ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Turns ────────────────────────────────────────────────────────

    def add_turn(
        self,
        episode_id: str,
        role: str,
        content: str,
        intent: str | None = None,
        tokens: int | None = None,
    ) -> int:
        """Add a turn and index it in FTS5. Returns turn id."""
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO turns (episode_id, role, content, intent, tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (episode_id, role, content, intent, tokens, now),
        )
        turn_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO session_fts (content, source_type, source_id) VALUES (?, ?, ?)",
            (content, "turn", str(turn_id)),
        )
        self.conn.execute(
            "UPDATE episodes SET updated_at = ? WHERE id = ?",
            (now, episode_id),
        )
        self.conn.commit()
        return turn_id

    def episode_turns(self, episode_id: str, limit: int | None = None) -> list[dict]:
        """Return turns for an episode. If limit, returns the most recent N."""
        if limit is not None:
            rows = self.conn.execute(
                "SELECT * FROM ("
                "  SELECT * FROM turns WHERE episode_id = ? "
                "  ORDER BY created_at DESC LIMIT ?"
                ") ORDER BY created_at ASC",
                (episode_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM turns WHERE episode_id = ? ORDER BY created_at ASC",
                (episode_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_turns(self, n: int = 5) -> list[dict]:
        """Most recent N turns across all episodes."""
        rows = self.conn.execute(
            "SELECT * FROM turns ORDER BY created_at DESC LIMIT ?", (n,),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Code refs ────────────────────────────────────────────────────

    def add_code_ref(
        self,
        episode_id: str,
        file_path: str,
        symbols: list[str] | None = None,
        ref_type: str = "read",
        turn_id: int | None = None,
    ) -> int:
        """Record a code reference. Returns ref id."""
        symbols_json = json.dumps(symbols) if symbols else None
        cur = self.conn.execute(
            "INSERT INTO code_refs (episode_id, turn_id, file_path, symbols, ref_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (episode_id, turn_id, file_path, symbols_json, ref_type),
        )
        self.conn.commit()
        return cur.lastrowid

    def episode_code_refs(self, episode_id: str) -> list[dict]:
        """Return all code refs for an episode."""
        rows = self.conn.execute(
            "SELECT * FROM code_refs WHERE episode_id = ?", (episode_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["symbols"] = json.loads(d["symbols"]) if d["symbols"] else None
            result.append(d)
        return result

    def files_touched(self, episode_id: str | None = None) -> set[str]:
        """All files referenced in an episode, or all episodes if None."""
        if episode_id is not None:
            rows = self.conn.execute(
                "SELECT DISTINCT file_path FROM code_refs WHERE episode_id = ?",
                (episode_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT file_path FROM code_refs",
            ).fetchall()
        return {r["file_path"] for r in rows}

    # ── Decisions ────────────────────────────────────────────────────

    def add_decision(
        self,
        episode_id: str,
        decision: str,
        rationale: str | None = None,
        turn_id: int | None = None,
        supersedes: int | None = None,
    ) -> int:
        """Record a decision. Returns decision id."""
        cur = self.conn.execute(
            "INSERT INTO decisions (episode_id, turn_id, decision, rationale, supersedes) "
            "VALUES (?, ?, ?, ?, ?)",
            (episode_id, turn_id, decision, rationale, supersedes),
        )
        dec_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO session_fts (content, source_type, source_id) VALUES (?, ?, ?)",
            (decision, "decision", str(dec_id)),
        )
        self.conn.commit()
        return dec_id

    def active_decisions(self, episode_id: str | None = None) -> list[dict]:
        """Return decisions not superseded by others."""
        if episode_id is not None:
            rows = self.conn.execute(
                "SELECT d.* FROM decisions d "
                "WHERE d.episode_id = ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM decisions s WHERE s.supersedes = d.id"
                ") ORDER BY d.id",
                (episode_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT d.* FROM decisions d "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM decisions s WHERE s.supersedes = d.id"
                ") ORDER BY d.id",
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Artifacts ────────────────────────────────────────────────────

    def add_artifact(
        self,
        episode_id: str,
        artifact_type: str,
        content: str,
        name: str | None = None,
        turn_id: int | None = None,
        tokens: int | None = None,
    ) -> int:
        """Store an artifact. Returns artifact id."""
        cur = self.conn.execute(
            "INSERT INTO artifacts (episode_id, turn_id, artifact_type, name, content, tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (episode_id, turn_id, artifact_type, name, content, tokens),
        )
        art_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO session_fts (content, source_type, source_id) VALUES (?, ?, ?)",
            (content, "artifact", str(art_id)),
        )
        self.conn.commit()
        return art_id

    def episode_artifacts(
        self, episode_id: str, artifact_type: str | None = None,
    ) -> list[dict]:
        """Return artifacts for an episode, optionally filtered by type."""
        if artifact_type is not None:
            rows = self.conn.execute(
                "SELECT * FROM artifacts WHERE episode_id = ? AND artifact_type = ?",
                (episode_id, artifact_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM artifacts WHERE episode_id = ?", (episode_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Search ───────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 search across turns, decisions, and artifacts."""
        rows = self.conn.execute(
            "SELECT content, source_type, source_id, "
            "  snippet(session_fts, 0, '>>>', '<<<', '...', 64) AS snippet, "
            "  rank "
            "FROM session_fts WHERE session_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ────────────────────────────────────────────────────────

    def session_stats(self) -> dict:
        """Total episodes, turns, tokens, code refs, decisions, artifacts."""
        row = self.conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM episodes) AS episodes, "
            "  (SELECT COUNT(*) FROM turns) AS turns, "
            "  (SELECT COALESCE(SUM(tokens), 0) FROM turns) AS total_tokens, "
            "  (SELECT COUNT(*) FROM code_refs) AS code_refs, "
            "  (SELECT COUNT(*) FROM decisions) AS decisions, "
            "  (SELECT COUNT(*) FROM artifacts) AS artifacts",
        ).fetchone()
        return dict(row)

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def reset(self, archive: bool = True) -> None:
        """Clear all data. If archive, copy DB to timestamped backup first."""
        self.conn.close()
        if archive and self.db_path.exists():
            ts = time.strftime("%Y%m%d_%H%M%S")
            backup = self.db_path.with_suffix(f".{ts}.bak")
            shutil.copy2(self.db_path, backup)
        if self.db_path.exists():
            self.db_path.unlink()
        self.conn = _connect(self.db_path)
