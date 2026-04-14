"""
stacks.py — Library and stack hinting for code generation.

Pure data module. No LLM calls. Keyword matching, not inference.

Three layers:
  1. Stack definitions   — curated tool combos (web-api, cli, data, etc.)
  2. Library registry    — known-good packages with version pins and import names
  3. Anti-patterns       — "don't use X, use Y instead" substitution table

Usage:
  from codeclub.stacks import resolve_stack, render_hints

  stack = resolve_stack("build a REST API that manages nvmeof devices")
  hints = render_hints(stack)  # inject into prompts
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════════════════
# Library specs — known-good packages
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Lib:
    """One known-good library with import path and version constraint."""
    name: str           # pip name
    import_name: str    # Python import (may differ from pip name)
    version: str        # minimum version constraint
    purpose: str        # one-line what it does
    notes: str = ""     # LLM-relevant usage notes


# ── Web / API ────────────────────────────────────────────────────────────────

FASTAPI = Lib("fastapi", "fastapi", ">=0.115", "async web framework",
              "Use fastapi.APIRouter for route groups. Pydantic v2 models for request/response.")
UVICORN = Lib("uvicorn", "uvicorn", ">=0.34", "ASGI server",
              "uvicorn.run(app, host='0.0.0.0', port=8000)")
PYDANTIC = Lib("pydantic", "pydantic", ">=2.10", "data validation",
               "Use model_validator, field_validator (not old validator decorator). "
               "Use model_dump() not dict().")
HTTPX = Lib("httpx", "httpx", ">=0.28", "async HTTP client",
            "Prefer over requests for async. httpx.AsyncClient() as context manager.")

# ── Database ─────────────────────────────────────────────────────────────────

SQLALCHEMY = Lib("sqlalchemy", "sqlalchemy", ">=2.0", "ORM + SQL toolkit",
                 "Use 2.0-style: select(), Session.execute(), Mapped[] type hints. "
                 "NOT Query() or session.query() (1.x style).")
ALEMBIC = Lib("alembic", "alembic", ">=1.14", "database migrations",
              "alembic init, alembic revision --autogenerate, alembic upgrade head.")
ASYNCPG = Lib("asyncpg", "asyncpg", ">=0.30", "async PostgreSQL driver",
              "Use with SQLAlchemy async: create_async_engine('postgresql+asyncpg://...')")
AIOSQLITE = Lib("aiosqlite", "aiosqlite", ">=0.20", "async SQLite driver",
                "create_async_engine('sqlite+aiosqlite:///...')")

# ── CLI ──────────────────────────────────────────────────────────────────────

TYPER = Lib("typer", "typer", ">=0.15", "CLI framework",
            "Typer is Click underneath. Use typer.Argument() and typer.Option(). "
            "app = typer.Typer(). @app.command().")
RICH = Lib("rich", "rich", ">=13.9", "terminal formatting",
           "rich.console.Console(), rich.table.Table(), rich.progress.track().")
CLICK = Lib("click", "click", ">=8.1", "CLI framework (lower-level)",
            "Use if Typer is too magic. @click.command(), @click.option().")

# ── Data ─────────────────────────────────────────────────────────────────────

POLARS = Lib("polars", "polars", ">=1.20", "fast DataFrames",
             "Prefer over pandas for new code. pl.read_csv(), df.filter(), df.group_by().")
DUCKDB = Lib("duckdb", "duckdb", ">=1.2", "embedded analytical DB",
             "duckdb.sql('SELECT ...').df() for one-liners. Reads parquet/csv natively.")

# ── Testing ──────────────────────────────────────────────────────────────────

PYTEST = Lib("pytest", "pytest", ">=8.3", "test framework",
             "Use fixtures, parametrize. No unittest.TestCase.")
HYPOTHESIS = Lib("hypothesis", "hypothesis", ">=6.115", "property-based testing",
                 "from hypothesis import given, strategies as st.")
RESPX = Lib("respx", "respx", ">=0.22", "mock httpx requests",
            "Use with httpx. @respx.mock decorator or respx.Router().")

# ── Async / system ───────────────────────────────────────────────────────────

ANYIO = Lib("anyio", "anyio", ">=4.7", "structured concurrency",
            "Prefer anyio.create_task_group() over raw asyncio.gather().")
STRUCTLOG = Lib("structlog", "structlog", ">=24.4", "structured logging",
                "structlog.get_logger(). Bind key=value context. JSON output in prod.")
PYDANTIC_SETTINGS = Lib("pydantic-settings", "pydantic_settings", ">=2.7",
                        "env-based config",
                        "class Settings(BaseSettings): model_config = SettingsConfigDict(env_file='.env')")

# ── Serialization ────────────────────────────────────────────────────────────

ORJSON = Lib("orjson", "orjson", ">=3.10", "fast JSON",
             "orjson.dumps() returns bytes. Use with FastAPI: ORJSONResponse.")
MSGSPEC = Lib("msgspec", "msgspec", ">=0.19", "fast serialization",
              "msgspec.Struct for schemas. msgspec.json.decode/encode.")


# ═══════════════════════════════════════════════════════════════════════════════
# Anti-patterns — "don't use X, use Y"
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AntiPattern:
    bad: str          # what the model might reach for
    good: str         # what to use instead
    reason: str       # why


ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern("flask", "fastapi",
                "FastAPI is async, has auto OpenAPI docs, Pydantic validation, and better LLM familiarity."),
    AntiPattern("django", "fastapi + sqlalchemy",
                "Django ORM is implicit. SQLAlchemy 2.0 explicit queries are easier for LLMs to generate correctly."),
    AntiPattern("requests", "httpx",
                "httpx supports async natively. Same API as requests but with async context manager."),
    AntiPattern("pandas", "polars",
                "Polars is faster, no index confusion, better API. Use polars for new code."),
    AntiPattern("argparse", "typer",
                "Typer generates help/completions from type hints. Less boilerplate, fewer bugs."),
    AntiPattern("print()", "rich.console.Console()",
                "Rich handles colors, tables, progress bars. Console.print() is a drop-in."),
    AntiPattern("logging.basicConfig", "structlog",
                "structlog gives structured JSON logs. Better for production, easier to filter."),
    AntiPattern("os.path", "pathlib.Path",
                "pathlib is the modern stdlib path API. Path() / 'subdir' / 'file.txt'."),
    AntiPattern("json.dumps", "orjson.dumps",
                "orjson is 10x faster. Returns bytes. Use for high-throughput JSON."),
    AntiPattern("unittest.TestCase", "pytest",
                "pytest is simpler: plain functions, fixtures, parametrize. No class needed."),
    AntiPattern("session.query()", "select()",
                "SQLAlchemy 2.0 uses select() + session.execute(). query() is legacy 1.x API."),
    AntiPattern("from pydantic import validator", "from pydantic import field_validator",
                "Pydantic v2 uses field_validator and model_validator. validator is v1 API."),
    AntiPattern(".dict()", ".model_dump()",
                "Pydantic v2 uses .model_dump() not .dict(). .dict() is deprecated."),
    AntiPattern("aiohttp", "httpx",
                "httpx has cleaner API, works sync and async. aiohttp callback style is error-prone."),
    AntiPattern("asyncio.gather", "anyio.create_task_group",
                "anyio task groups have structured cancellation. gather() swallows errors."),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Stack definitions — curated combos for common project types
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Stack:
    """A curated combination of libraries for a project type."""
    name: str
    description: str
    libs: list[Lib]
    test_libs: list[Lib]
    patterns: list[str]       # architecture patterns / conventions
    file_structure: str       # recommended project layout
    keywords: list[str]       # task keywords that trigger this stack


STACK_WEB_API = Stack(
    name="web-api",
    description="Async REST/GraphQL API with database",
    libs=[FASTAPI, UVICORN, PYDANTIC, PYDANTIC_SETTINGS, SQLALCHEMY, ALEMBIC, HTTPX, STRUCTLOG, ORJSON],
    test_libs=[PYTEST, HTTPX, RESPX],
    patterns=[
        "Repository pattern: db queries in repos/, business logic in services/, routes in routes/",
        "Pydantic models for all request/response schemas in schemas/",
        "Dependency injection via FastAPI Depends() for db sessions, auth, config",
        "Alembic migrations in migrations/versions/",
        "Settings from environment via pydantic-settings",
        "Async SQLAlchemy sessions: async_sessionmaker, AsyncSession",
    ],
    file_structure="""\
app/
├── main.py          # FastAPI app, lifespan, middleware
├── config.py        # pydantic-settings Settings class
├── db.py            # engine, async_sessionmaker, Base
├── models/          # SQLAlchemy ORM models (Mapped[] style)
├── schemas/         # Pydantic request/response models
├── repos/           # database query functions
├── services/        # business logic
├── routes/          # FastAPI APIRouter modules
└── deps.py          # Depends() factories (get_db, get_current_user)
migrations/
├── env.py
└── versions/
tests/
├── conftest.py      # async test client, test db fixtures
└── test_*.py""",
    keywords=["api", "rest", "endpoint", "server", "backend", "web", "http",
              "crud", "database", "postgres", "sqlite", "microservice", "webhook",
              "graphql", "fastapi"],
)

STACK_CLI = Stack(
    name="cli",
    description="Command-line tool with rich output",
    libs=[TYPER, RICH, PYDANTIC, STRUCTLOG, HTTPX],
    test_libs=[PYTEST],
    patterns=[
        "One Typer app with subcommands via app.command()",
        "Rich Console for all user-facing output (tables, progress, errors)",
        "Pydantic models for config files (YAML/TOML/JSON)",
        "Exit codes: 0=success, 1=user error, 2=system error",
        "Config from ~/.config/<app>/config.toml or env vars",
    ],
    file_structure="""\
<app>/
├── __init__.py
├── __main__.py      # entry: typer app
├── cli.py           # Typer commands
├── config.py        # pydantic Settings / TOML loader
├── core.py          # business logic (no CLI deps)
└── output.py        # Rich formatting helpers
tests/
└── test_*.py""",
    keywords=["cli", "command", "terminal", "tool", "utility", "manage",
              "admin", "script", "daemon", "service", "monitor", "devices",
              "nvme", "nvmeof", "disk", "network", "systemctl", "ctl"],
)

STACK_DATA = Stack(
    name="data",
    description="Data processing pipeline",
    libs=[POLARS, DUCKDB, PYDANTIC, STRUCTLOG, HTTPX],
    test_libs=[PYTEST, HYPOTHESIS],
    patterns=[
        "Polars lazy frames for large data: scan_csv → filter → collect",
        "DuckDB for SQL-over-files (parquet, csv) without a server",
        "Pydantic for schema validation on ingested records",
        "Immutable transforms: input → transform → output, no mutation",
    ],
    file_structure="""\
pipeline/
├── __init__.py
├── ingest.py        # read sources into Polars frames
├── transform.py     # business logic transforms
├── validate.py      # Pydantic schema validation
├── output.py        # write results (parquet, csv, db)
└── config.py
tests/
└── test_*.py""",
    keywords=["data", "pipeline", "etl", "csv", "parquet", "dataframe",
              "analytics", "transform", "ingest", "report", "dashboard",
              "aggregat", "metric"],
)

STACK_LIBRARY = Stack(
    name="library",
    description="Reusable Python package",
    libs=[PYDANTIC, STRUCTLOG],
    test_libs=[PYTEST, HYPOTHESIS],
    patterns=[
        "Public API in __init__.py, implementation in submodules",
        "Pydantic models for all public config/options dataclasses",
        "Type hints on all public functions",
        "Docstrings with Args/Returns/Raises sections",
        "No side effects on import",
    ],
    file_structure="""\
src/<package>/
├── __init__.py      # public API re-exports
├── core.py          # main logic
├── models.py        # Pydantic/dataclass models
├── exceptions.py    # custom exceptions
└── _internal.py     # private helpers
tests/
├── conftest.py
└── test_*.py
pyproject.toml""",
    keywords=["library", "package", "module", "sdk", "client", "wrapper",
              "abstraction", "interface", "plugin"],
)

STACK_ASYNC_SERVICE = Stack(
    name="async-service",
    description="Long-running async service (worker, consumer, scheduler)",
    libs=[ANYIO, HTTPX, PYDANTIC, PYDANTIC_SETTINGS, STRUCTLOG, SQLALCHEMY],
    test_libs=[PYTEST, RESPX],
    patterns=[
        "anyio task groups for structured concurrency",
        "Graceful shutdown via signal handlers + cancellation scopes",
        "Pydantic-settings for env config",
        "Health check endpoint (even for non-HTTP services)",
        "Structured logging with correlation IDs",
    ],
    file_structure="""\
service/
├── __init__.py
├── main.py          # entrypoint, signal handling, task group
├── config.py        # pydantic-settings
├── worker.py        # core async logic
├── health.py        # /health endpoint
└── models.py
tests/
└── test_*.py""",
    keywords=["worker", "consumer", "queue", "scheduler", "cron", "background",
              "event", "listener", "subscriber", "producer", "stream", "async",
              "concurrent", "parallel"],
)


ALL_STACKS = [STACK_WEB_API, STACK_CLI, STACK_DATA, STACK_LIBRARY, STACK_ASYNC_SERVICE]


# ═══════════════════════════════════════════════════════════════════════════════
# Stack resolution — keyword matching, not LLM
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_stack(task: str, stack_name: str | None = None) -> Stack:
    """
    Pick the best stack for a task.

    If stack_name is given explicitly, use that. Otherwise, score each stack
    by keyword hits in the task description. Falls back to 'library' if no
    keywords match.
    """
    if stack_name:
        for s in ALL_STACKS:
            if s.name == stack_name:
                return s
        raise ValueError(f"Unknown stack: {stack_name!r}. "
                         f"Available: {[s.name for s in ALL_STACKS]}")

    task_lower = task.lower()
    scores: list[tuple[int, Stack]] = []
    for s in ALL_STACKS:
        score = sum(1 for kw in s.keywords if kw in task_lower)
        scores.append((score, s))

    scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_stack = scores[0]

    # Fall back to library if nothing matches
    if best_score == 0:
        return STACK_LIBRARY

    return best_stack


def relevant_anti_patterns(stack: Stack) -> list[AntiPattern]:
    """Return anti-patterns relevant to this stack's library choices."""
    good_names = {lib.name for lib in stack.libs + stack.test_libs}
    good_imports = {lib.import_name for lib in stack.libs + stack.test_libs}
    result = []
    for ap in ANTI_PATTERNS:
        # Include if the "good" replacement is in our stack
        if ap.good in good_names or ap.good in good_imports:
            result.append(ap)
        # Also include universal anti-patterns (stdlib upgrades)
        if ap.bad in ("os.path", "print()", "logging.basicConfig",
                       "unittest.TestCase", "asyncio.gather"):
            result.append(ap)
    # deduplicate
    seen = set()
    deduped = []
    for ap in result:
        if ap.bad not in seen:
            seen.add(ap.bad)
            deduped.append(ap)
    return deduped


# ═══════════════════════════════════════════════════════════════════════════════
# Hint rendering — format for prompt injection
# ═══════════════════════════════════════════════════════════════════════════════

def render_hints(stack: Stack, *, include_structure: bool = True) -> str:
    """
    Render stack hints as a prompt block.

    Returns a <stack> XML block ready to inject into spec/map/fill prompts.
    This is DATA, not prose — the LLM reads it as constraints.
    """
    lines = [f'<stack name="{stack.name}">']

    # Libraries with versions and usage notes
    lines.append("USE THESE LIBRARIES (exact import names and versions):")
    for lib in stack.libs:
        line = f"  - {lib.import_name} ({lib.name}{lib.version}): {lib.purpose}"
        if lib.notes:
            line += f"\n    {lib.notes}"
        lines.append(line)

    lines.append("")
    lines.append("TEST WITH:")
    for lib in stack.test_libs:
        line = f"  - {lib.import_name} ({lib.name}{lib.version}): {lib.purpose}"
        if lib.notes:
            line += f"\n    {lib.notes}"
        lines.append(line)

    # Anti-patterns
    anti = relevant_anti_patterns(stack)
    if anti:
        lines.append("")
        lines.append("DO NOT USE (and what to use instead):")
        for ap in anti:
            lines.append(f"  ✗ {ap.bad} → ✓ {ap.good}: {ap.reason}")

    # Architecture patterns
    if stack.patterns:
        lines.append("")
        lines.append("ARCHITECTURE:")
        for p in stack.patterns:
            lines.append(f"  - {p}")

    # File structure
    if include_structure and stack.file_structure:
        lines.append("")
        lines.append("FILE STRUCTURE:")
        for fline in stack.file_structure.splitlines():
            lines.append(f"  {fline}")

    lines.append("</stack>")
    return "\n".join(lines)


def render_fill_hints(stack: Stack) -> str:
    """
    Compact hints for fill prompts (shorter — only imports and anti-patterns).

    Fill models see this per-function, so keep it tight.
    """
    lines = ["<imports>"]
    lines.append("Available libraries (use these, not alternatives):")
    for lib in stack.libs:
        lines.append(f"  import {lib.import_name}  # {lib.purpose}")
        if lib.notes:
            lines.append(f"    # {lib.notes}")
    anti = relevant_anti_patterns(stack)
    if anti:
        lines.append("Avoid:")
        for ap in anti:
            lines.append(f"  ✗ {ap.bad} → ✓ {ap.good}")
    lines.append("</imports>")
    return "\n".join(lines)


def render_test_hints(stack: Stack) -> str:
    """Hints for test generation — which test framework and mocking tools."""
    lines = ["<test_stack>"]
    for lib in stack.test_libs:
        line = f"  - {lib.import_name} ({lib.name}{lib.version}): {lib.purpose}"
        if lib.notes:
            line += f"\n    {lib.notes}"
        lines.append(line)
    # Add relevant anti-patterns for testing
    lines.append("  - Do NOT use unittest.TestCase. Use plain pytest functions + fixtures.")
    lines.append("</test_stack>")
    return "\n".join(lines)
