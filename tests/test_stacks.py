"""Tests for codeclub.stacks — stack resolution, hints rendering, anti-patterns."""

from codeclub.stacks import (
    resolve_stack,
    render_hints,
    render_fill_hints,
    render_test_hints,
    relevant_anti_patterns,
    ALL_STACKS,
    ANTI_PATTERNS,
    Stack,
)


class TestResolveStack:
    def test_explicit_name(self):
        s = resolve_stack("anything", stack_name="cli")
        assert s.name == "cli"

    def test_explicit_name_unknown_raises(self):
        import pytest
        with pytest.raises(ValueError, match="Unknown stack"):
            resolve_stack("anything", stack_name="nonexistent")

    def test_api_keywords(self):
        s = resolve_stack("build a REST API that manages nvmeof devices")
        assert s.name in ("web-api", "cli")  # "api" + "manage" + "devices"

    def test_cli_keywords(self):
        s = resolve_stack("build a CLI tool that monitors disk usage")
        assert s.name == "cli"

    def test_data_keywords(self):
        s = resolve_stack("ETL pipeline to transform CSV data into parquet")
        assert s.name == "data"

    def test_library_keywords(self):
        s = resolve_stack("create a Python SDK client library")
        assert s.name == "library"

    def test_async_keywords(self):
        s = resolve_stack("build a background worker that consumes from a queue")
        assert s.name == "async-service"

    def test_no_keywords_fallback(self):
        s = resolve_stack("do something completely abstract with no hints")
        assert s.name == "library"  # fallback


class TestRenderHints:
    def test_contains_stack_xml(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_hints(s)
        assert '<stack name="web-api">' in hints
        assert "</stack>" in hints

    def test_contains_libraries(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_hints(s)
        assert "fastapi" in hints
        assert "sqlalchemy" in hints
        assert "alembic" in hints

    def test_contains_anti_patterns(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_hints(s)
        assert "DO NOT USE" in hints
        assert "✗" in hints
        assert "✓" in hints

    def test_contains_architecture(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_hints(s)
        assert "ARCHITECTURE:" in hints

    def test_contains_file_structure(self):
        s = resolve_stack("", stack_name="cli")
        hints = render_hints(s)
        assert "FILE STRUCTURE:" in hints

    def test_no_structure_option(self):
        s = resolve_stack("", stack_name="cli")
        hints = render_hints(s, include_structure=False)
        assert "FILE STRUCTURE:" not in hints


class TestRenderFillHints:
    def test_compact_format(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_fill_hints(s)
        assert "<imports>" in hints
        assert "</imports>" in hints
        assert "import fastapi" in hints

    def test_includes_avoid(self):
        s = resolve_stack("", stack_name="web-api")
        hints = render_fill_hints(s)
        assert "Avoid:" in hints


class TestRenderTestHints:
    def test_test_stack_xml(self):
        s = resolve_stack("", stack_name="data")
        hints = render_test_hints(s)
        assert "<test_stack>" in hints
        assert "</test_stack>" in hints
        assert "pytest" in hints

    def test_hypothesis_for_data(self):
        s = resolve_stack("", stack_name="data")
        hints = render_test_hints(s)
        assert "hypothesis" in hints


class TestAntiPatterns:
    def test_relevant_anti_patterns_web(self):
        s = resolve_stack("", stack_name="web-api")
        anti = relevant_anti_patterns(s)
        bad_names = {ap.bad for ap in anti}
        assert "flask" in bad_names
        assert "requests" in bad_names

    def test_relevant_anti_patterns_cli(self):
        s = resolve_stack("", stack_name="cli")
        anti = relevant_anti_patterns(s)
        bad_names = {ap.bad for ap in anti}
        assert "argparse" in bad_names

    def test_no_duplicates(self):
        for s in ALL_STACKS:
            anti = relevant_anti_patterns(s)
            bads = [ap.bad for ap in anti]
            assert len(bads) == len(set(bads)), f"Duplicates in {s.name}: {bads}"


class TestAllStacksComplete:
    """Verify every stack has required fields filled."""

    def test_all_have_keywords(self):
        for s in ALL_STACKS:
            assert len(s.keywords) >= 3, f"{s.name} has too few keywords"

    def test_all_have_libs(self):
        for s in ALL_STACKS:
            assert len(s.libs) >= 1, f"{s.name} has no libs"

    def test_all_have_test_libs(self):
        for s in ALL_STACKS:
            assert len(s.test_libs) >= 1, f"{s.name} has no test libs"

    def test_all_have_patterns(self):
        for s in ALL_STACKS:
            assert len(s.patterns) >= 1, f"{s.name} has no patterns"

    def test_all_have_file_structure(self):
        for s in ALL_STACKS:
            assert s.file_structure.strip(), f"{s.name} has no file structure"
