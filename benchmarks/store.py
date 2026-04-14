"""
Persistent benchmark result store — SQLite-backed.

Results keyed by (model, task_id, task_hash) so:
- Changed tasks automatically invalidate old results
- Unchanged model×task combos are skipped on re-run
- Full generated code + diagnostics stored for post-mortem
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

DB_PATH = Path(__file__).parent / "results.db"


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_tag     TEXT NOT NULL,          -- human label ("v1", "gemini-rerun")
            started_at  REAL NOT NULL,          -- time.time()
            finished_at REAL,
            git_sha     TEXT,
            notes       TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS results (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        INTEGER REFERENCES runs(id),

            -- Keys: this triple determines uniqueness
            model         TEXT NOT NULL,
            task_id       TEXT NOT NULL,
            task_hash     TEXT NOT NULL,         -- content hash of spec+tests

            -- Task metadata
            task_name     TEXT,
            task_lang     TEXT,
            task_difficulty  INTEGER,
            coord_difficulty INTEGER,
            coord_clarity    INTEGER,

            -- Model metadata
            model_params_b  REAL,
            model_quant     TEXT,
            model_tok_s     REAL,
            model_is_local  INTEGER,

            -- Smash routing
            smash_fit     REAL,
            est_time_s    REAL,
            smash_measured INTEGER,

            -- Measured
            quality       REAL NOT NULL,
            tests_passed  INTEGER NOT NULL,
            tests_total   INTEGER NOT NULL,
            actual_time_s REAL,
            wall_time_s   REAL,
            cost_usd      REAL,
            tokens_in     INTEGER,
            tokens_out    INTEGER,
            energy_j      REAL,

            -- Diagnostics
            error         TEXT DEFAULT '',
            generated_code TEXT DEFAULT '',       -- full code, not truncated

            -- Timestamps
            created_at    REAL NOT NULL,

            UNIQUE(model, task_id, task_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_results_model ON results(model);
        CREATE INDEX IF NOT EXISTS idx_results_task  ON results(task_id);
        CREATE INDEX IF NOT EXISTS idx_results_quality ON results(quality);
    """)


class BenchStore:
    """Persistent benchmark result store."""

    def __init__(self, db_path: Path = DB_PATH):
        self.conn = _connect(db_path)

    def close(self):
        self.conn.close()

    # ── Runs ──────────────────────────────────────────────────────────

    def start_run(self, tag: str, git_sha: str = "", notes: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (run_tag, started_at, git_sha, notes) VALUES (?, ?, ?, ?)",
            (tag, time.time(), git_sha, notes),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ? WHERE id = ?",
            (time.time(), run_id),
        )
        self.conn.commit()

    # ── Results ───────────────────────────────────────────────────────

    def has_result(self, model: str, task_id: str, task_hash: str) -> bool:
        """Check if we already have a valid result for this model×task combo."""
        row = self.conn.execute(
            "SELECT 1 FROM results WHERE model=? AND task_id=? AND task_hash=?",
            (model, task_id, task_hash),
        ).fetchone()
        return row is not None

    def get_result(self, model: str, task_id: str, task_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM results WHERE model=? AND task_id=? AND task_hash=?",
            (model, task_id, task_hash),
        ).fetchone()
        return dict(row) if row else None

    def store_result(self, run_id: int | None, *, model: str, task_id: str,
                     task_hash: str, **kwargs) -> None:
        """Upsert a benchmark result."""
        existing = self.get_result(model, task_id, task_hash)
        if existing:
            # Update in place
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [model, task_id, task_hash]
            self.conn.execute(
                f"UPDATE results SET run_id=?, {sets} WHERE model=? AND task_id=? AND task_hash=?",
                [run_id] + vals,
            )
        else:
            cols = ["run_id", "model", "task_id", "task_hash", "created_at"] + list(kwargs.keys())
            placeholders = ", ".join(["?"] * len(cols))
            vals = [run_id, model, task_id, task_hash, time.time()] + list(kwargs.values())
            self.conn.execute(
                f"INSERT INTO results ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        self.conn.commit()

    def invalidate(self, model: str = "", task_id: str = "") -> int:
        """Delete results matching filter. Returns count deleted."""
        clauses, vals = [], []
        if model:
            clauses.append("model = ?"); vals.append(model)
        if task_id:
            clauses.append("task_id = ?"); vals.append(task_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        cur = self.conn.execute(f"DELETE FROM results WHERE {where}", vals)
        self.conn.commit()
        return cur.rowcount

    # ── Queries ───────────────────────────────────────────────────────

    def all_results(self, model: str = "", task_id: str = "") -> list[dict]:
        """Get results with optional filters."""
        clauses, vals = [], []
        if model:
            clauses.append("model = ?"); vals.append(model)
        if task_id:
            clauses.append("task_id = ?"); vals.append(task_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.conn.execute(
            f"SELECT * FROM results WHERE {where} ORDER BY task_difficulty, model", vals
        ).fetchall()
        return [dict(r) for r in rows]

    def model_summary(self) -> list[dict]:
        """Per-model aggregate stats."""
        rows = self.conn.execute("""
            SELECT model,
                   COUNT(*) as fights,
                   SUM(CASE WHEN quality >= 1.0 THEN 1 ELSE 0 END) as perfect,
                   AVG(quality) as avg_quality,
                   SUM(cost_usd) as total_cost,
                   AVG(actual_time_s) as avg_time_s
            FROM results GROUP BY model ORDER BY avg_quality DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def missing_combos(self, models: list[str], tasks: dict) -> list[tuple[str, str]]:
        """Return (model, task_id) pairs that need running."""
        missing = []
        for model in models:
            for tid, task in tasks.items():
                if not self.has_result(model, tid, task.content_hash):
                    missing.append((model, tid))
        return missing

    def export_json(self, path: Path) -> int:
        """Export all results as JSON (for backward compat with old scripts)."""
        results = self.all_results()
        # Strip internal fields
        for r in results:
            r.pop("id", None)
            r.pop("run_id", None)
            r.pop("created_at", None)
            r.pop("generated_code", None)  # too big for export
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=2))
        return len(results)

    def import_json(self, path: Path, run_id: int | None = None) -> int:
        """Import results from old-format JSON files."""
        data = json.loads(path.read_text())
        count = 0
        for r in data:
            task_id = r.get("task_id", "")
            model = r.get("model", "")
            # Need task hash — look it up from TASKS if possible
            from tournament import TASKS
            task = TASKS.get(task_id)
            if not task:
                continue
            task_hash = task.content_hash

            if self.has_result(model, task_id, task_hash):
                continue

            self.store_result(
                run_id,
                model=model,
                task_id=task_id,
                task_hash=task_hash,
                task_name=r.get("task_name", ""),
                task_lang=r.get("task_lang", ""),
                task_difficulty=r.get("task_difficulty", 0),
                coord_difficulty=r.get("coord_difficulty", 0),
                coord_clarity=r.get("coord_clarity", 0),
                model_params_b=r.get("model_params_b"),
                model_quant=r.get("model_quant", ""),
                model_tok_s=r.get("model_tok_s"),
                model_is_local=1 if r.get("model_is_local") else 0,
                smash_fit=r.get("smash_fit", 0),
                est_time_s=r.get("est_time_s"),
                smash_measured=r.get("smash_measured", 0),
                quality=r.get("quality", 0),
                tests_passed=r.get("tests_passed", 0),
                tests_total=r.get("tests_total", 0),
                actual_time_s=r.get("actual_time_s"),
                wall_time_s=r.get("wall_time_s"),
                cost_usd=r.get("cost_usd"),
                tokens_in=r.get("tokens_in"),
                tokens_out=r.get("tokens_out"),
                energy_j=r.get("energy_j"),
                error=r.get("error", ""),
                generated_code=r.get("code_snippet", ""),
            )
            count += 1
        return count
