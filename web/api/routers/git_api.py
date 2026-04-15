"""Git integration for task worktrees, branches, diffs, commits, and PRs."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from ..database import DB_PATH, get_db, log_activity
from ..models import (
    BranchInfo,
    CommitRequest,
    CommitResponse,
    DiffResponse,
    PRResponse,
    WorktreeCreateRequest,
    WorktreeInfo,
)

router = APIRouter()
TASK_WORKTREE_ROOT = Path("/tmp/codeclub-tasks")
TASK_BRANCH_RE = re.compile(r"^task/[^/]+-.+")
DEFAULT_BASE_BRANCH = "main"
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


def _validate_ref(value: str, label: str = "ref") -> str:
    """Reject refs that could be interpreted as CLI flags or contain dangerous chars."""
    if not value or not _SAFE_REF_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: must start with alphanumeric, contain only [A-Za-z0-9_./-]",
        )
    return value


def _repo_root() -> Path:
    return DB_PATH.resolve().parents[2]


async def _run_git(*args: str, cwd: str | None = None) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


async def _run_gh(*args: str, cwd: str | None = None) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode(), proc.returncode


def _command_error(action: str, stderr: str, stdout: str = "") -> HTTPException:
    detail = (stderr or stdout).strip() or f"{action} failed"
    return HTTPException(status_code=400, detail=detail)


def _slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:30].strip("-")
    return slug or "task"


def _is_task_branch(branch: str | None) -> bool:
    return bool(branch and TASK_BRANCH_RE.match(branch))


def _is_task_worktree(path: str) -> bool:
    prefix = f"{TASK_WORKTREE_ROOT}/"
    return path == str(TASK_WORKTREE_ROOT) or path.startswith(prefix)


def _parse_worktree_list(output: str) -> list[WorktreeInfo]:
    worktrees: list[WorktreeInfo] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        path = current["path"]
        branch = current.get("branch")
        worktrees.append(
            WorktreeInfo(
                path=path,
                branch=branch,
                head=current.get("head", ""),
                is_task=_is_task_worktree(path),
            )
        )
        current.clear()

    for line in output.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                flush()
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
    flush()
    return worktrees


def _parse_branch_list(output: str) -> list[BranchInfo]:
    branches: list[BranchInfo] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        name, short_sha, date = line.split(maxsplit=2)
        branches.append(
            BranchInfo(
                name=name,
                short_sha=short_sha,
                date=date,
                is_task_branch=_is_task_branch(name),
            )
        )
    return branches


def _parse_numstat(output: str) -> tuple[int, int, int]:
    files_changed = 0
    insertions = 0
    deletions = 0

    for line in output.splitlines():
        parts = line.split("\t", maxsplit=2)
        if len(parts) < 3:
            continue
        added, removed, _ = parts
        files_changed += 1
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)

    return files_changed, insertions, deletions


async def _get_task_or_404(
    db: aiosqlite.Connection, task_id: str
) -> aiosqlite.Row:
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


async def _resolve_head(cwd: str) -> str:
    stdout, stderr, code = await _run_git("rev-parse", "HEAD", cwd=cwd)
    if code != 0:
        raise _command_error("Resolve HEAD", stderr, stdout)
    return stdout.strip()


@router.get("/", response_model=list[WorktreeInfo])
async def list_worktrees():
    stdout, stderr, code = await _run_git(
        "worktree",
        "list",
        "--porcelain",
        cwd=str(_repo_root()),
    )
    if code != 0:
        raise _command_error("List worktrees", stderr, stdout)
    return _parse_worktree_list(stdout)


@router.get("/branches", response_model=list[BranchInfo])
async def list_branches():
    stdout, stderr, code = await _run_git(
        "branch",
        "--format=%(refname:short) %(objectname:short) %(committerdate:iso8601)",
        "--sort=-committerdate",
        cwd=str(_repo_root()),
    )
    if code != 0:
        raise _command_error("List branches", stderr, stdout)
    return _parse_branch_list(stdout)


@router.post("/worktree", response_model=WorktreeInfo, status_code=201)
async def create_worktree(
    body: WorktreeCreateRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    task = await _get_task_or_404(db, body.task_id)
    if task["worktree_path"] or task["branch"]:
        raise HTTPException(status_code=409, detail="Task already has git metadata")

    _validate_ref(body.base_branch, "base_branch")
    branch = f"task/{body.task_id[:8]}-{_slugify_title(task['title'])}"
    path = TASK_WORKTREE_ROOT / body.task_id
    path.parent.mkdir(parents=True, exist_ok=True)

    stdout, stderr, code = await _run_git(
        "worktree",
        "add",
        "-b",
        branch,
        str(path),
        body.base_branch,
        cwd=str(_repo_root()),
    )
    if code != 0:
        raise _command_error("Create worktree", stderr, stdout)

    head = await _resolve_head(str(path))
    await db.execute(
        "UPDATE tasks SET branch = ?, worktree_path = ? WHERE id = ?",
        (branch, str(path), body.task_id),
    )
    await log_activity(
        db,
        "git_worktree_created",
        "task",
        body.task_id,
        {
            "branch": branch,
            "worktree_path": str(path),
            "base_branch": body.base_branch,
        },
    )
    await db.commit()
    return WorktreeInfo(path=str(path), branch=branch, head=head, is_task=True)


@router.delete("/worktree/{task_id}")
async def remove_worktree(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    task = await _get_task_or_404(db, task_id)
    path = task["worktree_path"] or str(TASK_WORKTREE_ROOT / task_id)

    stdout, stderr, code = await _run_git(
        "worktree",
        "remove",
        path,
        "--force",
        cwd=str(_repo_root()),
    )
    if code != 0:
        raise _command_error("Remove worktree", stderr, stdout)

    await db.execute(
        "UPDATE tasks SET branch = NULL, worktree_path = NULL WHERE id = ?",
        (task_id,),
    )
    await log_activity(
        db,
        "git_worktree_removed",
        "task",
        task_id,
        {"worktree_path": path},
    )
    await db.commit()
    return {"ok": True}


@router.get("/diff/{task_id}", response_model=DiffResponse)
async def get_diff(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    task = await _get_task_or_404(db, task_id)

    if task["worktree_path"]:
        cwd = task["worktree_path"]
        diff_args = ("diff", "HEAD")
        stats_args = ("diff", "--numstat", "HEAD")
    elif task["branch"]:
        cwd = str(_repo_root())
        refspec = f"{DEFAULT_BASE_BRANCH}...{task['branch']}"
        diff_args = ("diff", refspec)
        stats_args = ("diff", "--numstat", refspec)
    else:
        raise HTTPException(status_code=409, detail="Task has no branch or worktree")

    diff, stderr, code = await _run_git(*diff_args, cwd=cwd)
    if code != 0:
        raise _command_error("Get diff", stderr, diff)

    stats, stats_stderr, stats_code = await _run_git(*stats_args, cwd=cwd)
    if stats_code != 0:
        raise _command_error("Get diff stats", stats_stderr, stats)

    files_changed, insertions, deletions = _parse_numstat(stats)
    return DiffResponse(
        diff=diff,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
    )


@router.post("/commit/{task_id}", response_model=CommitResponse)
async def commit_changes(
    task_id: str,
    body: CommitRequest,
    db: aiosqlite.Connection = Depends(get_db),
):
    task = await _get_task_or_404(db, task_id)
    worktree_path = task["worktree_path"]
    if not worktree_path:
        raise HTTPException(status_code=409, detail="Task has no worktree")

    stdout, stderr, code = await _run_git("add", "-A", cwd=worktree_path)
    if code != 0:
        raise _command_error("Stage changes", stderr, stdout)

    stdout, stderr, code = await _run_git(
        "commit",
        "-m",
        body.message,
        cwd=worktree_path,
    )
    if code != 0:
        raise _command_error("Commit changes", stderr, stdout)

    sha = await _resolve_head(worktree_path)
    await log_activity(
        db,
        "git_commit_created",
        "task",
        task_id,
        {"sha": sha, "message": body.message},
    )
    await db.commit()
    return CommitResponse(sha=sha, message=body.message)


@router.post("/pr/{task_id}", response_model=PRResponse, status_code=201)
async def create_pr(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    task = await _get_task_or_404(db, task_id)
    if not task["branch"]:
        raise HTTPException(status_code=409, detail="Task has no branch")
    if task["pr_url"]:
        raise HTTPException(status_code=409, detail="Task already has a PR")

    stdout, stderr, code = await _run_gh(
        "pr",
        "create",
        "--head",
        task["branch"],
        "--title",
        task["title"],
        "--body",
        task["description"],
        cwd=str(_repo_root()),
    )
    if code != 0:
        raise _command_error("Create PR", stderr, stdout)

    combined_output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    pr_url = next(
        (line.strip() for line in combined_output.splitlines() if "http" in line),
        "",
    )
    if not pr_url:
        raise HTTPException(status_code=500, detail="PR created but URL was not returned")

    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        raise HTTPException(status_code=500, detail="Could not parse PR number")

    pr_number = int(match.group(1))
    await db.execute("UPDATE tasks SET pr_url = ? WHERE id = ?", (pr_url, task_id))
    await log_activity(
        db,
        "git_pr_created",
        "task",
        task_id,
        {"branch": task["branch"], "pr_url": pr_url, "pr_number": pr_number},
    )
    await db.commit()
    return PRResponse(pr_url=pr_url, pr_number=pr_number)
