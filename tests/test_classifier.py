"""Tests for codeclub.context.classifier — intent classification, clarity, refs."""

from codeclub.context.classifier import (
    Classification,
    Intent,
    classify,
    estimate_clarity,
    extract_file_refs,
    extract_symbol_refs,
    should_start_new_episode,
)


# ── Intent classification ────────────────────────────────────────────


class TestNewTaskIntent:
    def test_create_command(self):
        c = classify("Create a REST API for managing users")
        assert c.intent == Intent.NEW_TASK

    def test_build_from_scratch(self):
        c = classify("Build a rate limiter from scratch")
        assert c.intent == Intent.NEW_TASK

    def test_implement_feature(self):
        c = classify("Implement a token bucket algorithm")
        assert c.intent == Intent.NEW_TASK


class TestFollowUpIntent:
    def test_fix_something(self):
        c = classify("Fix the validation logic in the handler")
        assert c.intent == Intent.FOLLOW_UP

    def test_update_code(self):
        c = classify("Update the serializer to handle nulls")
        assert c.intent == Intent.FOLLOW_UP

    def test_tweak(self):
        c = classify("Tweak the timeout to 30 seconds")
        assert c.intent == Intent.FOLLOW_UP


class TestDebugIntent:
    def test_error_keyword(self):
        c = classify("I'm getting an error when I run the tests")
        assert c.intent == Intent.DEBUG

    def test_traceback(self):
        c = classify('Traceback: File "foo.py", line 42, in bar\n  TypeError: bad arg')
        assert c.intent == Intent.DEBUG

    def test_not_working(self):
        c = classify("The parser doesn't work anymore, it crashes on empty input")
        assert c.intent == Intent.DEBUG


class TestQuestionIntent:
    def test_how_question(self):
        c = classify("How does the authentication middleware work?")
        assert c.intent == Intent.QUESTION

    def test_why_question(self):
        c = classify("Why are we using SQLite instead of Postgres?")
        assert c.intent == Intent.QUESTION

    def test_explain(self):
        c = classify("Explain the caching strategy")
        assert c.intent == Intent.QUESTION


class TestRefactorIntent:
    def test_rename(self):
        c = classify("Rename the UserManager class to UserService")
        assert c.intent == Intent.REFACTOR

    def test_extract_function(self):
        c = classify("Extract the validation logic into a separate function")
        assert c.intent == Intent.REFACTOR

    def test_clean_up(self):
        c = classify("Clean up the duplicate code in the handlers")
        assert c.intent == Intent.REFACTOR


class TestReviewIntent:
    def test_review_code(self):
        c = classify("Review the changes in the auth module")
        assert c.intent == Intent.REVIEW

    def test_audit(self):
        c = classify("Audit the security of the API endpoints")
        assert c.intent == Intent.REVIEW


class TestExploreIntent:
    def test_find_files(self):
        c = classify("Find all files that import the config module")
        assert c.intent == Intent.EXPLORE

    def test_where_is(self):
        c = classify("Where is the database connection configured?")
        assert c.intent in (Intent.EXPLORE, Intent.QUESTION)

    def test_show_me(self):
        c = classify("Show me all the test fixtures")
        assert c.intent == Intent.EXPLORE


class TestContinueIntent:
    def test_yes(self):
        c = classify("yes")
        assert c.intent == Intent.CONTINUE

    def test_go_ahead(self):
        c = classify("go ahead")
        assert c.intent == Intent.CONTINUE

    def test_ship_it(self):
        c = classify("ship it")
        assert c.intent == Intent.CONTINUE

    def test_sounds_good(self):
        c = classify("sounds good")
        assert c.intent == Intent.CONTINUE

    def test_long_message_not_continue(self):
        c = classify("yes I think we should also add error handling and logging")
        assert c.intent != Intent.CONTINUE


class TestPivotIntent:
    def test_actually_prefix(self):
        c = classify("Actually, let's work on the database layer instead")
        assert c.intent == Intent.PIVOT
        assert c.is_pivot is True

    def test_forget_that(self):
        c = classify("Forget that, let's switch to testing")
        assert c.intent == Intent.PIVOT
        assert c.is_pivot is True

    def test_topic_divergence(self):
        recent = [
            {"role": "user", "content": "Let's build the authentication system"},
            {"role": "assistant", "content": "Sure, I'll create the auth module"},
        ]
        c = classify(
            "Can you set up the Kubernetes deployment manifests?",
            recent_context=recent,
        )
        assert c.is_pivot is True


class TestMetaIntent:
    def test_summary(self):
        c = classify("Give me a summary of what we've done")
        assert c.intent == Intent.META

    def test_status(self):
        c = classify("What's the status of this session?")
        assert c.intent == Intent.META


# ── Clarity estimation ───────────────────────────────────────────────


class TestClarityEstimation:
    def test_high_clarity(self):
        msg = (
            "Implement a `TokenBucket` class in rate_limiter.py that "
            "uses a token bucket algorithm. It should have `consume(n)` "
            "returning bool and `refill()` methods. Returns False when "
            "tokens exhausted. Test: assert bucket.consume(5) is True."
        )
        score = estimate_clarity(msg)
        assert score >= 70, f"Expected high clarity >=70, got {score}"

    def test_mid_clarity(self):
        msg = (
            "Add a caching layer to the API responses so repeated "
            "queries don't hit the database every time"
        )
        score = estimate_clarity(msg)
        assert 30 <= score <= 70, f"Expected mid clarity 30-70, got {score}"

    def test_low_clarity(self):
        score = estimate_clarity("make it faster")
        assert score <= 40, f"Expected low clarity <=40, got {score}"

    def test_very_low_clarity(self):
        score = estimate_clarity("build something")
        assert score < 20, f"Expected very low clarity <20, got {score}"

    def test_capped_at_bounds(self):
        assert estimate_clarity("x") >= 5
        huge = (
            "Implement a `BinarySearch` class in search.py with a "
            "`find(target)` method that returns int index. Uses the "
            "binary search algorithm on sorted arrays. Test case: "
            "```python\nassert searcher.find(42) == 3\n``` "
            "Returns -1 when not found. Uses async iterator with "
            "middleware callback for queue processing on the endpoint."
        )
        assert estimate_clarity(huge) <= 95


# ── File and symbol extraction ───────────────────────────────────────


class TestFileExtraction:
    def test_python_file(self):
        refs = extract_file_refs("Look at foo.py for the config")
        assert "foo.py" in refs

    def test_nested_path(self):
        refs = extract_file_refs("Edit src/auth/middleware.ts")
        assert "src/auth/middleware.ts" in refs

    def test_backtick_path(self):
        refs = extract_file_refs("Check `utils/helpers.py` for details")
        assert "utils/helpers.py" in refs

    def test_version_excluded(self):
        refs = extract_file_refs("Upgrade to v2.0 or 3.1.4")
        assert not refs

    def test_multiple_files(self):
        refs = extract_file_refs("Compare main.py and test_main.py")
        assert "main.py" in refs
        assert "test_main.py" in refs


class TestSymbolExtraction:
    def test_camel_case(self):
        syms = extract_symbol_refs("Rename the UserManager class")
        assert "UserManager" in syms

    def test_backtick_symbol(self):
        syms = extract_symbol_refs("Call `process_batch` with the data")
        assert "process_batch" in syms

    def test_explicit_ref(self):
        syms = extract_symbol_refs("the Config class needs updating")
        assert "Config" in syms

    def test_no_duplicates(self):
        syms = extract_symbol_refs("UserService and UserService again")
        assert syms.count("UserService") == 1


# ── Pivot detection ──────────────────────────────────────────────────


class TestPivotDetection:
    def test_no_pivot_without_context(self):
        c = classify("Add logging to the handler")
        assert c.is_pivot is False

    def test_same_topic_not_pivot(self):
        recent = [
            {"role": "user", "content": "Let's add logging to the server"},
            {"role": "assistant", "content": "I'll add structured logging"},
        ]
        c = classify("Also add logging to the client", recent_context=recent)
        assert c.is_pivot is False


# ── Episode boundaries ───────────────────────────────────────────────


class TestEpisodeBoundary:
    def test_pivot_starts_new_episode(self):
        c = Classification(
            intent=Intent.PIVOT, confidence=0.9, clarity=50,
            file_refs=[], symbol_refs=[], is_pivot=True,
        )
        assert should_start_new_episode(c, current_episode_intent="follow_up")

    def test_idle_timeout(self):
        c = Classification(
            intent=Intent.FOLLOW_UP, confidence=0.8, clarity=50,
            file_refs=[], symbol_refs=[], is_pivot=False,
        )
        assert should_start_new_episode(
            c, current_episode_intent="follow_up", current_episode_age_s=600,
        )

    def test_new_task_after_debug(self):
        c = Classification(
            intent=Intent.NEW_TASK, confidence=0.8, clarity=60,
            file_refs=[], symbol_refs=[], is_pivot=False,
        )
        assert should_start_new_episode(c, current_episode_intent="debug")

    def test_follow_up_stays_in_episode(self):
        c = Classification(
            intent=Intent.FOLLOW_UP, confidence=0.8, clarity=50,
            file_refs=[], symbol_refs=[], is_pivot=False,
        )
        assert not should_start_new_episode(
            c, current_episode_intent="follow_up", current_episode_age_s=60,
        )

    def test_no_current_episode_starts_new(self):
        c = Classification(
            intent=Intent.FOLLOW_UP, confidence=0.8, clarity=50,
            file_refs=[], symbol_refs=[], is_pivot=False,
        )
        assert should_start_new_episode(c, current_episode_intent=None)


# ── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_message(self):
        c = classify("")
        assert c.intent == Intent.FOLLOW_UP
        assert c.confidence < 0.2

    def test_whitespace_only(self):
        c = classify("   \n\t  ")
        assert c.intent == Intent.FOLLOW_UP

    def test_very_long_message(self):
        msg = "Fix the bug in the parser. " * 200
        c = classify(msg)
        assert c.intent in (Intent.FOLLOW_UP, Intent.DEBUG)
        assert isinstance(c.confidence, float)

    def test_mixed_signals(self):
        c = classify("Create a fix for the broken authentication error")
        assert c.intent in (Intent.NEW_TASK, Intent.DEBUG, Intent.FOLLOW_UP)
        assert c.confidence > 0

    def test_classification_has_all_fields(self):
        c = classify("Implement a linked list")
        assert isinstance(c.intent, Intent)
        assert 0.0 <= c.confidence <= 1.0
        assert 5 <= c.clarity <= 95
        assert isinstance(c.file_refs, list)
        assert isinstance(c.symbol_refs, list)
        assert isinstance(c.is_pivot, bool)
        assert isinstance(c.keywords, dict)
