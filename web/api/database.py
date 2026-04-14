"""
SQLite database — schema, migrations, connection factory.

Single file DB at web/data/codeclub.db. Created on first startup.
JSON-in-TEXT columns for flexible nested data (like Ozma dev-manager).
Non-destructive migrations via ALTER TABLE ADD COLUMN in try/except.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent.parent / "data" / "codeclub.db"

SCHEMA = """
-- Task queue
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 50,
    setup TEXT DEFAULT 'best_local_first',
    stack TEXT,
    language TEXT DEFAULT 'python',
    budget TEXT DEFAULT 'cheap',
    complexity TEXT,
    git_enabled INTEGER DEFAULT 0,
    branch TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    final_code TEXT,
    test_output TEXT,
    review_json TEXT,
    ledger_json TEXT,
    phases_json TEXT DEFAULT '[]',
    error TEXT,
    fix_rounds INTEGER DEFAULT 0,
    max_fix_rounds INTEGER DEFAULT 5,
    map_model TEXT,
    fill_model TEXT,
    review_model TEXT,
    parent_task_id TEXT REFERENCES tasks(id),
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

-- Run history (every execution attempt)
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    attempt INTEGER DEFAULT 1,
    status TEXT,
    phases_json TEXT DEFAULT '[]',
    code_snapshot TEXT,
    test_output TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    elapsed_s REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Tournament fight results
CREATE TABLE IF NOT EXISTS tournament_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    model TEXT NOT NULL,
    mapper TEXT,
    quality REAL,
    tests_passed INTEGER,
    tests_total INTEGER,
    elapsed_s REAL,
    cost_usd REAL,
    energy_j REAL,
    smash_fit REAL,
    smash_measured INTEGER,
    fitness REAL,
    final_code TEXT,
    test_details TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Endpoint health snapshots
CREATE TABLE IF NOT EXISTS endpoint_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    alive INTEGER,
    response_ms REAL,
    checked_at TEXT DEFAULT (datetime('now'))
);

-- Key-value settings (like Ozma app_settings)
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Activity log (lightweight event sourcing)
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    detail TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# Default settings seeded on first init
DEFAULTS = {
    "default_setup": "best_local_first",
    "default_budget": "cheap",
    "default_language": "python",
    "pipeline_paused": "false",
    "pipeline_concurrency": "1",
    "git_enabled_default": "false",
}


async def init_db() -> None:
    """Create tables and seed defaults. Safe to call repeatedly."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Seed defaults (INSERT OR IGNORE = don't overwrite existing)
        for key, value in DEFAULTS.items():
            await db.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def get_db():
    """FastAPI dependency — yields an aiosqlite connection."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def log_activity(
    db: aiosqlite.Connection,
    event: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write to the activity log."""
    await db.execute(
        "INSERT INTO activity_log (event, entity_type, entity_id, detail) "
        "VALUES (?, ?, ?, ?)",
        (event, entity_type, entity_id, json.dumps(detail) if detail else None),
    )


async def get_setting(db: aiosqlite.Connection, key: str) -> str | None:
    """Read a setting value."""
    cursor = await db.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    )
    row = await cursor.fetchone()
    return row["value"] if row else None


async def set_setting(db: aiosqlite.Connection, key: str, value: str) -> None:
    """Write a setting value."""
    await db.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
        (key, value),
    )
