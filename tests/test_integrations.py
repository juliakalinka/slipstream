"""Тести адаптерів CI-серверів і розбору звітів тестів."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.integrations import GitHubActionsAdapter, GitLabAdapter, get_adapter
from app.integrations.base import CIAdapter
from app.integrations.junit import parse_directory, parse_string

PYTEST_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1" time="6.75">
    <testcase classname="tests.auth.test_login" name="test_valid_credentials" time="0.412"/>
    <testcase classname="tests.auth.test_login" name="test_expired_token" time="1.204">
      <failure message="AssertionError: expected 401, got 200">traceback...</failure>
    </testcase>
    <testcase classname="tests.billing.test_invoice" name="test_totals" time="4.100">
      <error message="ConnectionError: database unavailable"/>
    </testcase>
    <testcase classname="tests.billing.test_invoice" name="test_currency" time="0.0">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
"""

SINGLE_SUITE = """<testsuite name="jest" tests="2" time="1.5">
  <testcase classname="ui.Button" name="renders" time="0.5"/>
  <testcase classname="ui.Button" name="handles click" time="1.0"><failure/></testcase>
</testsuite>
"""


# ------------------------------------------------------------- розбір JUnit
def test_parses_pytest_report():
    r = parse_string(PYTEST_REPORT)
    assert len(r.tests) == 4
    assert r.passed == 1
    assert r.failed == 2, "і failure, і error вважаються падінням"

    by_name = {t.name: t for t in r.tests}
    assert by_name["test_valid_credentials"].status == "passed"
    assert by_name["test_expired_token"].status == "failed"
    assert by_name["test_totals"].status == "failed"
    assert by_name["test_currency"].status == "skipped"


def test_durations_converted_to_milliseconds():
    r = parse_string(PYTEST_REPORT)
    by_name = {t.name: t for t in r.tests}
    assert by_name["test_valid_credentials"].duration_ms == 412
    assert by_name["test_totals"].duration_ms == 4100


def test_suite_is_module_not_package_root():
    """Для tests.auth.test_login набором має бути test_login, а не tests."""
    r = parse_string(PYTEST_REPORT)
    assert {t.suite for t in r.tests} == {"test_login", "test_invoice"}


def test_file_path_reconstructed():
    r = parse_string(PYTEST_REPORT)
    paths = {t.file_path for t in r.tests}
    assert "tests/auth/test_login.py" in paths


def test_error_message_captured():
    r = parse_string(PYTEST_REPORT)
    failed = next(t for t in r.tests if t.name == "test_expired_token")
    assert "401" in failed.error


def test_single_testsuite_root_supported():
    r = parse_string(SINGLE_SUITE)
    assert len(r.tests) == 2
    assert r.failed == 1


def test_malformed_xml_returns_empty_not_crash():
    """Недоступний або зіпсований звіт не має валити приймання події."""
    assert parse_string("це не xml").tests == []
    assert parse_string("").tests == []
    assert parse_string("<testsuites><broken>").tests == []


def test_payload_conversion_matches_webhook_schema():
    from app.schemas import WebhookEvent
    r = parse_string(PYTEST_REPORT)
    payload = r.to_payload(job_name="test:shard-1")
    assert all({"suite", "name", "status", "duration_ms"} <= set(p) for p in payload)
    assert all(p["job"] == "test:shard-1" for p in payload)
    # схема вебхука має прийняти цей формат без зауважень
    ev = WebhookEvent(
        project_id=1, event="pipeline_finished", external_id="t1",
        commit_sha="a" * 40, test_results=payload,
    )
    assert len(ev.test_results) == 4


def test_parse_directory_merges_shards(tmp_path):
    """Кожен шард пише свій звіт, система має зібрати їх разом."""
    (tmp_path / "shard-1.xml").write_text(SINGLE_SUITE)
    (tmp_path / "shard-2.xml").write_text(SINGLE_SUITE)
    (tmp_path / "ignore.txt").write_text("не звіт")
    r = parse_directory(tmp_path)
    assert len(r.tests) == 4
    assert r.failed == 2


# ------------------------------------------------------------------ адаптери
def test_factory_returns_correct_adapter():
    assert isinstance(get_adapter("gitlab", "https://gitlab.local", "tok", "42"), GitLabAdapter)
    assert isinstance(get_adapter("github", "", "tok", "owner/repo"), GitHubActionsAdapter)


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError, match="немає адаптера"):
        get_adapter("bamboo", "https://x", "", "1")


def test_adapters_implement_full_interface():
    """Кожен адаптер має реалізувати весь контракт CIAdapter."""
    for adapter in (GitLabAdapter("https://g", "t", "1"),
                    GitHubActionsAdapter("https://api.github.com", "t", "o/r")):
        assert isinstance(adapter, CIAdapter)
        for method in ("fetch_pipelines", "fetch_jobs", "fetch_commit",
                       "fetch_test_report", "verify_signature", "health"):
            assert callable(getattr(adapter, method))


def test_github_does_not_promise_test_reports():
    """Документоване обмеження: API GitHub Actions не віддає окремих тестів."""
    gh = GitHubActionsAdapter("https://api.github.com", "t", "o/r")
    assert gh.fetch_test_report("123") == ""


def test_github_webhook_signature_verification():
    import hashlib
    import hmac
    gh = GitHubActionsAdapter("https://api.github.com", "", "o/r")
    payload, secret = b'{"action":"completed"}', "s3cret"
    good = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert gh.verify_signature(payload, good, secret) is True
    assert gh.verify_signature(payload, good, "wrong-secret") is False
    assert gh.verify_signature(b'{"action":"tampered"}', good, secret) is False
    assert gh.verify_signature(payload, "", secret) is False


def test_gitlab_status_mapping():
    from app.integrations.gitlab import STATUS_MAP
    assert STATUS_MAP["success"] == "success"
    assert STATUS_MAP["failed"] == "failed"
    assert STATUS_MAP["pending"] == "running"
    assert STATUS_MAP["skipped"] == "canceled"


def test_github_conclusion_mapping_covers_real_values():
    from app.integrations.github import CONCLUSION_MAP
    for real in ("success", "failure", "cancelled", "timed_out",
                 "startup_failure", "skipped", "neutral"):
        assert real in CONCLUSION_MAP, f"невідомий висновок GitHub: {real}"
    assert CONCLUSION_MAP["timed_out"] == "failed"
    assert CONCLUSION_MAP["cancelled"] == "canceled"


def test_health_returns_false_when_server_unreachable():
    """Недоступний CI-сервер не має призводити до винятку."""
    gl = GitLabAdapter("http://127.0.0.1:9", "tok", "1")
    assert gl.health() is False
