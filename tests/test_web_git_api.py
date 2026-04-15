"""Tests for web.api.routers.git_api helpers."""
from web.api.routers.git_api import (
    _parse_branch_list,
    _parse_numstat,
    _parse_worktree_list,
    _slugify_title,
)


class TestSlugifyTitle:
    def test_normalizes_and_truncates(self):
        slug = _slugify_title("Build Git Integration Backend API!!! For Tasks")
        assert slug == "build-git-integration-backend"

    def test_empty_slug_falls_back_to_task(self):
        assert _slugify_title("!!!") == "task"


class TestParseWorktreeList:
    def test_marks_task_worktrees(self):
        output = (
            "worktree /home/matt/work/codeclub\n"
            "HEAD abcdef1234567890\n"
            "branch refs/heads/main\n\n"
            "worktree /tmp/codeclub-tasks/task-123\n"
            "HEAD fedcba0987654321\n"
            "branch refs/heads/task/task-123-git-api\n"
        )

        worktrees = _parse_worktree_list(output)

        assert [wt.path for wt in worktrees] == [
            "/home/matt/work/codeclub",
            "/tmp/codeclub-tasks/task-123",
        ]
        assert worktrees[0].is_task is False
        assert worktrees[1].is_task is True
        assert worktrees[1].branch == "task/task-123-git-api"


class TestParseBranchList:
    def test_marks_task_branches(self):
        branches = _parse_branch_list(
            "main abc1234 2025-04-15 10:00:00 +0000\n"
            "task/12345678-git-api def5678 2025-04-14 09:00:00 +0000\n"
        )

        assert branches[0].is_task_branch is False
        assert branches[1].is_task_branch is True
        assert branches[1].date == "2025-04-14 09:00:00 +0000"


class TestParseNumstat:
    def test_sums_diff_stats(self):
        files_changed, insertions, deletions = _parse_numstat(
            "10\t2\tweb/api/models.py\n"
            "5\t0\tweb/api/routers/git_api.py\n"
            "-\t-\timage.png\n"
        )

        assert files_changed == 3
        assert insertions == 15
        assert deletions == 2
