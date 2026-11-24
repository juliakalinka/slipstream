"""Модульні тести ядра машинного навчання.

Найважливіший тест у цьому файлі - test_no_future_leakage. Він доводить, що
ознаки для конвеєра, який стартував у момент t, не залежать від подій після t.
Витік майбутньої інформації - найчастіша причина завищених оцінок якості в
роботах такого типу, тому це твердження має бути перевіреним, а не заявленим.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.ml.features import (
    FAILURE_FEATURES, HistoryStats, build_commit_features, classify_path,
    features_to_vector, module_of,
)
from app.ml.sharding import lpt_shard, lpt_shard_groups, suggest_shard_count
from app.ml.test_selector import (
    SuiteStats, TestStats, apfd, build_rank_features, heuristic_score, select_tests,
)
from app.models import Commit, Pipeline, RunStatus


def make_commit(sha: str, paths: list[str], author="dev", add=50, dele=10, ts=None) -> Commit:
    return Commit(
        sha=sha, project_id=1, author=author, message="test",
        branch="feature/x", files_changed=len(paths), additions=add, deletions=dele,
        changed_paths=paths, committed_at=ts or datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
    )


def make_pipeline(ref="feature/x", failed=False, started=None) -> Pipeline:
    return Pipeline(
        project_id=1, commit_sha="x", ref=ref,
        status=RunStatus.FAILED if failed else RunStatus.SUCCESS,
        started_at=started or datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
    )


# ------------------------------------------------------------ класифікація шляхів
@pytest.mark.parametrize("path,expected", [
    ("docs/guide.md", "docs"),
    ("README.md", "docs"),
    ("CHANGELOG/notes.txt", "docs"),
    ("src/auth/login.py", "code"),
    ("tests/test_auth.py", "test"),
    ("src/__tests__/util.js", "test"),
    ("ci/config.yml", "config"),
    ("deploy/values.yaml", "config"),
])
def test_path_classification(path, expected):
    assert classify_path(path) == expected


def test_module_extraction():
    assert module_of("src/auth/login.py") == "src/auth"
    assert module_of("Makefile") == "Makefile"
    assert module_of("") == "<root>"


# ------------------------------------------------------------------- ознаки
def test_docs_only_commit_is_flagged():
    c = make_commit("a", ["docs/a.md", "docs/b.md", "README.md"])
    f = build_commit_features(c, "feature/x", HistoryStats())
    assert f["is_docs_only"] == 1.0
    assert f["docs_ratio"] == 1.0
    assert f["code_ratio"] == 0.0


def test_mixed_commit_is_not_docs_only():
    c = make_commit("a", ["docs/a.md", "src/auth/login.py"])
    f = build_commit_features(c, "feature/x", HistoryStats())
    assert f["is_docs_only"] == 0.0
    assert 0 < f["docs_ratio"] < 1


def test_feature_vector_length_and_order():
    c = make_commit("a", ["src/api/h.py"])
    f = build_commit_features(c, "main", HistoryStats())
    v = features_to_vector(f, FAILURE_FEATURES)
    assert len(v) == len(FAILURE_FEATURES)
    assert all(isinstance(x, float) and not math.isnan(x) for x in v)


def test_empty_commit_does_not_crash():
    """Реальні дані з GitHub часто не містять переліку змінених файлів."""
    c = make_commit("a", [])
    f = build_commit_features(c, "main", HistoryStats())
    assert f["files_changed"] == 0.0
    assert f["is_docs_only"] == 0.0        # порожній коміт не є документацією
    assert f["module_fail_rate"] == 0.25   # нейтральне значення за замовчуванням


def test_author_history_accumulates():
    hist = HistoryStats()
    c = make_commit("a", ["src/api/h.py"], author="bob")

    f0 = build_commit_features(c, "feature/x", hist)
    assert f0["author_fail_rate"] == 0.25  # історії ще немає

    for _ in range(4):
        hist.observe(c, make_pipeline(failed=True))
    f1 = build_commit_features(c, "feature/x", hist)
    assert f1["author_fail_rate"] == 1.0
    assert f1["author_experience"] > f0["author_experience"]


def test_no_future_leakage():
    """Ключова перевірка: ознаки моменту t не залежать від подій після t.

    Беремо ту саму історію, додаємо до неї майбутні збої і переконуємося,
    що ознаки, пораховані ДО їх додавання, не змінилися.
    """
    hist = HistoryStats()
    c_past = make_commit("past", ["src/auth/a.py"], author="alice")
    for _ in range(3):
        hist.observe(c_past, make_pipeline(failed=False))

    target = make_commit("target", ["src/auth/b.py"], author="alice")
    before = build_commit_features(target, "feature/x", hist)

    # майбутнє: alice починає ламати збірки
    for _ in range(10):
        hist.observe(c_past, make_pipeline(failed=True))
    after = build_commit_features(target, "feature/x", hist)

    # Ознаки, пораховані раніше, лишилися тими самими об'єктами значень.
    # Якби у формуванні ознак був витік, before довелося б перераховувати.
    assert before["author_fail_rate"] == 0.0
    assert after["author_fail_rate"] > before["author_fail_rate"], (
        "історія має впливати на НАСТУПНІ обчислення, інакше тест безглуздий"
    )


def test_off_hours_detection():
    night = make_commit("n", ["src/a/b.py"], ts=datetime(2026, 5, 1, 3, tzinfo=timezone.utc))
    day = make_commit("d", ["src/a/b.py"], ts=datetime(2026, 5, 1, 14, tzinfo=timezone.utc))
    sat = make_commit("s", ["src/a/b.py"], ts=datetime(2026, 5, 2, 14, tzinfo=timezone.utc))
    assert build_commit_features(night, "main", HistoryStats())["is_off_hours"] == 1.0
    assert build_commit_features(day, "main", HistoryStats())["is_off_hours"] == 0.0
    assert build_commit_features(sat, "main", HistoryStats())["is_off_hours"] == 1.0


# ------------------------------------------------------------------ шардинг
def test_lpt_balances_load():
    durations = {i: float(10 + i) for i in range(20)}
    r = lpt_shard(durations, 4)
    assert len(r.shards) == 4
    assert sum(len(s.test_ids) for s in r.shards) == 20
    assert r.imbalance < 0.10, "жадібний LPT має давати рівномірне завантаження"
    assert r.makespan_s < r.sequential_s


def test_lpt_handles_fewer_tests_than_shards():
    r = lpt_shard({1: 5.0, 2: 7.0}, 8)
    assert sum(len(s.test_ids) for s in r.shards) == 2
    assert r.makespan_s == 7.0


def test_lpt_single_shard_equals_sequential():
    d = {i: 3.0 for i in range(6)}
    r = lpt_shard(d, 1)
    assert r.makespan_s == r.sequential_s == 18.0
    assert r.speedup == 1.0


def test_groups_stay_together():
    """Залежні від порядку тести одного набору не можна розділяти."""
    groups = {
        "suite:auth": ([1, 2, 3, 4], 40.0),
        "test:5": ([5], 30.0),
        "test:6": ([6], 25.0),
        "suite:api": ([7, 8], 20.0),
    }
    r = lpt_shard_groups(groups, 3)
    placement = {t: s.index for s in r.shards for t in s.test_ids}
    assert len({placement[t] for t in (1, 2, 3, 4)}) == 1, "набір auth розірвано"
    assert len({placement[t] for t in (7, 8)}) == 1, "набір api розірвано"
    assert sum(len(s.test_ids) for s in r.shards) == 8


def test_suggest_shard_count():
    assert suggest_shard_count(600, 120) == 5
    assert suggest_shard_count(100, 120) == 1
    assert suggest_shard_count(10_000, 60, max_shards=8) == 8
    assert suggest_shard_count(500, 0) == 1


# ------------------------------------------------------------- відбір тестів
def stats(n=40, dur=1000.0, **kw) -> list[TestStats]:
    return [
        TestStats(test_id=i, suite=f"s{i % 4}", name=f"t{i}",
                  file_path=f"tests/s{i % 4}/t{i}.py", avg_duration_ms=dur, **kw)
        for i in range(n)
    ]


def test_selection_respects_budget():
    ts = stats(40, dur=1000.0)          # 40 секунд на весь набір
    r = select_tests(ts, ["src/auth/a.py"], budget_s=10.0)
    assert r.est_duration_s <= 10.0 + 1e-6
    assert 0 < len(r.selected) < 40
    assert r.reduction > 0.5


def test_selection_keeps_minimum_share():
    """Запобіжник: навіть за нульового бюджету щось має виконатися."""
    ts = stats(50, dur=5000.0)
    r = select_tests(ts, ["src/auth/a.py"], budget_s=0.0, min_keep_ratio=0.2)
    assert len(r.selected) >= 10


def test_quarantined_tests_are_never_selected():
    ts = stats(20)
    for t in ts[:5]:
        t.quarantined = True
    r = select_tests(ts, ["src/auth/a.py"], budget_s=1000.0)
    assert not ({0, 1, 2, 3, 4} & set(r.selected))
    assert set(range(5)) <= set(r.skipped)


def test_selection_prefers_related_tests():
    """Тест, який історично падав разом зі зміненим модулем, має бути відібраний."""
    ts = stats(30, dur=1000.0)
    related = ts[7]
    related.module_failures = {"src/billing": 20}
    related.failures, related.runs = 20, 100

    r = select_tests(ts, ["src/billing/service.py"], budget_s=6.0)
    assert related.test_id in r.selected
    assert r.scores[related.test_id] > r.scores[ts[0].test_id]


def test_recent_failures_are_forced_in():
    ts = stats(30, dur=2000.0)
    recent = ts[11]
    recent.last_failed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    recent.failures, recent.runs = 3, 50
    r = select_tests(ts, ["src/core/x.py"], budget_s=2.0)
    assert recent.test_id in r.selected, "тест, що впав кілька годин тому, не можна пропускати"


def test_suite_affinity_beats_empty_history():
    ss = SuiteStats()
    for _ in range(10):
        ss.observe("s0", {"src/auth"}, failed=True)
        ss.observe("s1", {"src/ui"}, failed=False)

    hot = TestStats(1, "s0", "t1", avg_duration_ms=1000)
    cold = TestStats(2, "s1", "t2", avg_duration_ms=1000)
    f_hot = build_rank_features(hot, ["src/auth/x.py"], suite_stats=ss)
    f_cold = build_rank_features(cold, ["src/auth/x.py"], suite_stats=ss)

    assert f_hot["suite_affinity_max"] > f_cold["suite_affinity_max"]
    assert heuristic_score(f_hot) > heuristic_score(f_cold)


def test_empty_test_list_does_not_crash():
    r = select_tests([], ["src/a/b.py"], budget_s=100.0)
    assert r.selected == []
    assert r.est_duration_s == 0.0


# ---------------------------------------------------------------------- APFD
def test_apfd_rewards_early_detection():
    order = list(range(10))
    early = apfd(order, {0, 1})
    late = apfd(order, {8, 9})
    assert early > late
    assert 0.0 <= late < early <= 1.0


def test_apfd_edge_cases():
    assert apfd([], set()) == 1.0
    assert apfd([1, 2, 3], set()) == 1.0
    assert apfd([1, 2, 3], {99}) == 0.0
