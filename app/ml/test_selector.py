"""Відбір перевірок, релевантних зміні.

Для кожної перевірки оцінюється ймовірність падіння саме на цій зміні. Ознаки:
історія падінь при зміні тих самих модулів (для перевірки і для її набору),
власна частота й давність падінь, близькість шляху перевірки до змінених
файлів. Відбір жадібний: за спаданням оцінки, віднесеної до тривалості, доки
не вичерпано бюджет часу.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

import numpy as np

from app.ml.features import module_of

RANK_FEATURES = [
    "affinity", "affinity_max", "suite_affinity", "suite_affinity_max",
    "fail_rate", "suite_fail_rate", "recency", "runs_log",
    "path_overlap", "same_module", "avg_duration_log", "flaky_score",
    "changed_modules",
]


class SuiteStats:
    """Історія падінь на рівні набору перевірок.

    Окрема перевірка падає рідко, тому зв'язок «модуль - перевірка» розріджений.
    На рівні набору той самий зв'язок накопичується швидше.
    """

    def __init__(self) -> None:
        self.module_failures: dict[str, dict[str, int]] = {}
        self.runs: dict[str, int] = {}
        self.failures: dict[str, int] = {}

    def observe(self, suite: str, modules: set[str], failed: bool) -> None:
        self.runs[suite] = self.runs.get(suite, 0) + 1
        if failed:
            self.failures[suite] = self.failures.get(suite, 0) + 1
            mf = self.module_failures.setdefault(suite, {})
            for m in modules:
                mf[m] = mf.get(m, 0) + 1

    def affinity(self, suite: str, modules: set[str]) -> tuple[float, float]:
        mf = self.module_failures.get(suite, {})
        total = sum(mf.values()) or 1
        vals = [mf.get(m, 0) / total for m in modules]
        return (sum(vals), max(vals) if vals else 0.0)

    def fail_rate(self, suite: str) -> float:
        r = self.runs.get(suite, 0)
        return self.failures.get(suite, 0) / r if r else 0.05


@dataclass
class TestStats:
    """Історія однієї перевірки."""
    __test__ = False  # не збирати як тестовий клас pytest

    test_id: int
    suite: str
    name: str
    file_path: str = ""
    runs: int = 0
    failures: int = 0
    avg_duration_ms: float = 1000.0
    flaky_score: float = 0.0
    last_failed_at: datetime | None = None
    quarantined: bool = False
    # перевірку не можна переставляти й відокремлювати від свого набору
    order_dependent: bool = False
    # скільки разів перевірка падала при зміні кожного модуля
    module_failures: dict[str, int] = field(default_factory=dict)

    @property
    def fail_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.05


@dataclass
class SelectionResult:
    selected: list[int]
    skipped: list[int]
    scores: dict[int, float]
    est_duration_s: float
    full_duration_s: float
    reason: str = ""

    @property
    def reduction(self) -> float:
        if not self.full_duration_s:
            return 0.0
        return 1 - self.est_duration_s / self.full_duration_s


def _path_overlap(test_path: str, changed_paths: list[str]) -> float:
    """Найбільша частка спільного початку шляху перевірки і зміненого файлу."""
    if not test_path or not changed_paths:
        return 0.0
    tp = PurePosixPath(test_path).parts
    best = 0.0
    for c in changed_paths:
        cp = PurePosixPath(c).parts
        common = 0
        for a, b in zip(tp, cp):
            if a != b:
                break
            common += 1
        best = max(best, common / max(len(tp), len(cp), 1))
    return best


def build_rank_features(
    ts: TestStats,
    changed_paths: list[str],
    now: datetime | None = None,
    suite_stats: "SuiteStats | None" = None,
) -> dict[str, float]:
    """Ознаки перевірки відносно зміни."""
    now = now or datetime.now(timezone.utc)
    modules = {module_of(p) for p in changed_paths}

    # спорідненість зі зміненими модулями: частка падінь перевірки, що
    # припадає на ці модулі (сумарна й найбільша)
    total_mf = sum(ts.module_failures.values()) or 1
    aff_vals = [ts.module_failures.get(m, 0) / total_mf for m in modules]
    affinity = sum(aff_vals)
    affinity_max = max(aff_vals) if aff_vals else 0.0

    if ts.last_failed_at:
        lf = ts.last_failed_at
        if not lf.tzinfo:
            lf = lf.replace(tzinfo=timezone.utc)
        days = max((now - lf).total_seconds() / 86400, 0.0)
        recency = math.exp(-days / 14.0)   # згасає приблизно за два тижні
    else:
        recency = 0.0

    if suite_stats is not None:
        s_aff, s_aff_max = suite_stats.affinity(ts.suite, modules)
        s_fail = suite_stats.fail_rate(ts.suite)
    else:
        s_aff = s_aff_max = 0.0
        s_fail = 0.05

    test_module = module_of(ts.file_path) if ts.file_path else ""
    return {
        "affinity": affinity,
        "affinity_max": affinity_max,
        "suite_affinity": s_aff,
        "suite_affinity_max": s_aff_max,
        "fail_rate": ts.fail_rate,
        "suite_fail_rate": s_fail,
        "recency": recency,
        "runs_log": math.log1p(ts.runs),
        "path_overlap": _path_overlap(ts.file_path, changed_paths),
        "same_module": 1.0 if test_module and test_module in modules else 0.0,
        "avg_duration_log": math.log1p(ts.avg_duration_ms / 1000.0),
        "flaky_score": ts.flaky_score,
        "changed_modules": float(len(modules)),
    }


def heuristic_score(f: dict[str, float]) -> float:
    """Оцінка без навченої моделі: зважена сума ознак, зведена до (0, 1).

    Ваги задано експертно. Це базова лінія, з якою порівнюється модель.
    """
    s = (
        1.8 * f["affinity_max"]
        + 1.0 * f["affinity"]
        + 2.0 * f.get("suite_affinity_max", 0.0)
        + 1.2 * f.get("suite_affinity", 0.0)
        + 1.4 * f["fail_rate"]
        + 1.0 * f.get("suite_fail_rate", 0.0)
        + 1.0 * f["recency"]
        + 0.8 * f["path_overlap"]
        + 0.6 * f["same_module"]
        - 0.5 * f["flaky_score"]
    )
    return 1 / (1 + math.exp(-s + 1.0))


class TestRanker:
    """Модель ранжування; без навченої моделі працює евристична оцінка."""

    def __init__(self, model=None, feature_names: list[str] | None = None):
        self.model = model
        self.features = feature_names or RANK_FEATURES

    def score_many(self, feats: list[dict[str, float]]) -> list[float]:
        if not feats:
            return []
        if self.model is None:
            return [heuristic_score(f) for f in feats]
        rows = [[f.get(k, 0.0) for k in self.features] for f in feats]
        X = np.asarray(rows, dtype=float)
        return [float(p) for p in self.model.predict_proba(X)[:, 1]]


def select_tests(
    stats: list[TestStats],
    changed_paths: list[str],
    budget_s: float,
    ranker: TestRanker | None = None,
    min_keep_ratio: float = 0.10,
    always_include_recent_fails: bool = True,
    now: datetime | None = None,
    suite_stats: "SuiteStats | None" = None,
) -> SelectionResult:
    """Відібрати перевірки під бюджет часу budget_s.

    Перевірки в карантині не розглядаються. Ті, що падали нещодавно і не є
    нестабільними, включаються поза бюджетом. За будь-якого бюджету лишається
    не менше ніж min_keep_ratio набору.
    """
    active = [t for t in stats if not t.quarantined]
    quarantined = [t.test_id for t in stats if t.quarantined]
    full_duration = sum(t.avg_duration_ms for t in stats) / 1000.0

    if not active:
        return SelectionResult(
            [], quarantined, {}, 0.0, full_duration, "all tests are quarantined"
        )

    ranker = ranker or TestRanker()
    feats = [
        build_rank_features(t, changed_paths, now, suite_stats) for t in active
    ]
    scores = ranker.score_many(feats)
    by_id = {t.test_id: s for t, s in zip(active, scores)}

    # питома корисність: оцінка на секунду виконання, тривалість не менше 0,05 с
    ranked = sorted(
        zip(active, scores, feats),
        key=lambda z: z[1] / max(z[0].avg_duration_ms / 1000.0, 0.05),
        reverse=True,
    )

    must: set[int] = set()
    if always_include_recent_fails:
        must = {
            t.test_id for t, _, f in ranked
            if f["recency"] > 0.5 and t.flaky_score < 0.3
        }

    selected: list[int] = []
    spent = 0.0
    for t, _, _ in ranked:
        if t.test_id in must:
            selected.append(t.test_id)
            spent += t.avg_duration_ms / 1000.0
    for t, _, _ in ranked:
        if t.test_id in must:
            continue
        cost = t.avg_duration_ms / 1000.0
        if spent + cost <= budget_s:
            selected.append(t.test_id)
            spent += cost

    # найменша частка набору, яка виконується завжди
    min_count = max(1, int(len(active) * min_keep_ratio))
    if len(selected) < min_count:
        chosen = set(selected)
        for t, _, _ in ranked:
            if len(selected) >= min_count:
                break
            if t.test_id not in chosen:
                selected.append(t.test_id)
                chosen.add(t.test_id)
                spent += t.avg_duration_ms / 1000.0

    chosen = set(selected)
    skipped = [t.test_id for t in active if t.test_id not in chosen] + quarantined
    return SelectionResult(
        selected=selected,
        skipped=skipped,
        scores=by_id,
        est_duration_s=spent,
        full_duration_s=full_duration,
        reason=(
            f"selected {len(selected)} of {len(stats)} "
            f"within a {budget_s:.0f}s budget"
        ),
    )


def apfd(order: list[int], failing: set[int]) -> float:
    """APFD: що ближче до 1, то раніше в послідовності виявлено дефекти."""
    n, m = len(order), len(failing)
    if n == 0 or m == 0:
        return 1.0
    positions = [i + 1 for i, tid in enumerate(order) if tid in failing]
    if not positions:
        return 0.0
    return 1 - sum(positions) / (n * m) + 1 / (2 * n)
