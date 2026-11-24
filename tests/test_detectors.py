"""Тести детекторів нестабільних тестів і аномалій конвеєра."""
from __future__ import annotations

import numpy as np
import pytest

from app.ml.anomaly import ANOMALY_FEATURES, check_pipeline, robust_z, train_detector
from app.models import AlertKind, Severity


# ------------------------------------------------------- робастний z-показник
def test_robust_z_ignores_outliers_in_history():
    """Медіана і MAD стійкі до викидів, на відміну від середнього і дисперсії.

    Історія має природний розкид, інакше MAD дорівнює нулю і порівнювати нічого.
    """
    rng = __import__("random").Random(3)
    clean = [rng.gauss(300, 20) for _ in range(40)]
    polluted = clean + [5000.0, 6000.0]     # два різкі викиди в самій історії

    z_clean = robust_z(360.0, clean)
    z_polluted = robust_z(360.0, polluted)
    assert z_clean == pytest.approx(z_polluted, rel=0.25), (
        "викиди в історії не повинні помітно зсувати оцінку"
    )

    # для порівняння: класичний z-показник на тих самих даних поїде сильно
    import statistics
    def plain_z(v, h):
        sd = statistics.pstdev(h)
        return (v - statistics.fmean(h)) / sd if sd else 0.0
    assert abs(plain_z(360.0, clean) - plain_z(360.0, polluted)) > abs(z_clean - z_polluted)


def test_robust_z_detects_spike():
    hist = [300.0 + i % 7 for i in range(40)]
    assert robust_z(305.0, hist) < 3.0
    assert robust_z(900.0, hist) > 10.0


def test_robust_z_needs_enough_history():
    assert robust_z(999.0, [100.0, 101.0]) == 0.0


def test_robust_z_handles_zero_variance():
    """Якщо історія стала, поділу на нуль бути не повинно."""
    assert robust_z(100.0, [100.0] * 20) == 0.0
    assert np.isfinite(robust_z(200.0, [100.0] * 20))


# --------------------------------------------------------- перевірка конвеєра
def base_features(duration=300.0, queue=10.0):
    return {"duration_s": duration, "queue_time_s": queue,
            "n_jobs": 8.0, "fail_ratio": 0.0, "n_tests": 1.0}


def test_normal_pipeline_is_not_flagged():
    hist = [300.0 + i % 9 for i in range(40)]
    v = check_pipeline(base_features(308.0), hist, [10.0] * 40)
    assert v.is_anomaly is False


def test_duration_spike_is_flagged_as_critical():
    hist = [300.0 + i % 9 for i in range(40)]
    v = check_pipeline(base_features(2400.0), hist, [10.0] * 40)
    assert v.is_anomaly is True
    assert v.kind == AlertKind.DURATION_SPIKE
    assert v.severity == Severity.CRITICAL
    assert "2400" in v.message or "с" in v.message


def test_tiny_deviation_on_stable_history_is_not_an_alert():
    """Реальна вада, знайдена тестом: за дуже стабільної історії MAD прямує
    до нуля, тому відхилення на кілька відсотків давало критичне сповіщення.
    Практичний поріг має це зняти."""
    hist = [300.0 + i % 5 for i in range(40)]        # майже стала історія
    v = check_pipeline(base_features(320.0), hist, [10.0] * 40)
    assert v.is_anomaly is False, "20 секунд різниці не варті тривоги"


def test_moderate_spike_is_warning_not_critical():
    hist = [300.0 + i % 5 for i in range(40)]
    v = check_pipeline(base_features(420.0), hist, [10.0] * 40)   # на 40% довше
    assert v.is_anomaly is True
    assert v.severity == Severity.WARNING


def test_doubled_duration_is_critical():
    hist = [300.0 + i % 5 for i in range(40)]
    v = check_pipeline(base_features(700.0), hist, [10.0] * 40)
    assert v.severity == Severity.CRITICAL


def test_fast_pipeline_is_not_alerted_on_seconds():
    """Конвеєр на 12 секунд не має піднімати тривогу через 6 секунд різниці."""
    hist = [12.0 + (i % 3) * 0.2 for i in range(40)]
    v = check_pipeline(base_features(18.0), hist, [2.0] * 40)
    assert v.is_anomaly is False


def test_queue_delay_detected_separately():
    dur_hist = [300.0 + i % 9 for i in range(40)]
    queue_hist = [8.0 + i % 3 for i in range(40)]
    v = check_pipeline(base_features(305.0, queue=900.0), dur_hist, queue_hist)
    assert v.is_anomaly is True
    assert v.kind == AlertKind.QUEUE_DELAY


def test_short_history_produces_no_false_alarms():
    v = check_pipeline(base_features(5000.0), [300.0, 310.0], [10.0, 11.0])
    assert v.is_anomaly is False, "на трьох точках історії висновків робити не можна"


def test_isolation_forest_flags_expected_share():
    rng = np.random.default_rng(42)
    X = np.column_stack([
        rng.normal(300, 25, 400), rng.normal(12, 3, 400),
        rng.integers(6, 10, 400), rng.random(400) * 0.2, np.ones(400),
    ])
    det = train_detector(X, contamination=0.05)
    flagged = (det.predict(X) == -1).sum()
    assert 0.02 * len(X) <= flagged <= 0.09 * len(X)
    assert len(ANOMALY_FEATURES) == X.shape[1]


def test_detector_catches_injected_outlier():
    rng = np.random.default_rng(7)
    X = np.column_stack([
        rng.normal(300, 20, 300), rng.normal(10, 2, 300),
        np.full(300, 8.0), np.zeros(300), np.ones(300),
    ])
    det = train_detector(X, contamination=0.03)
    outlier = np.array([[5000.0, 900.0, 40.0, 0.9, 1.0]])
    assert det.predict(outlier)[0] == -1


# ------------------------------------------ детектор нестабільних тестів на БД
def test_flaky_detector_finds_planted_tests():
    """Перевірка проти еталонної розмітки синтетичного стенда."""
    import json
    from pathlib import Path

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.ml.flaky import compute_flaky_scores
    from app.models import CIProvider, Project

    truth_file = Path("data/flaky_ground_truth.json")
    if not truth_file.exists():
        pytest.skip("немає еталонної розмітки: запустіть seed_synthetic.py")

    db = SessionLocal()
    try:
        project = db.scalars(
            select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)
        ).first()
        if project is None:
            pytest.skip("немає синтетичного стенда")

        truth = {int(k) for k in json.load(open(truth_file))}
        verdicts = compute_flaky_scores(db, project.id)
        flagged = {v.test_case_id for v in verdicts if v.should_quarantine}

        tp = len(flagged & truth)
        precision = tp / len(flagged) if flagged else 0.0
        recall = tp / len(truth) if truth else 0.0

        assert precision >= 0.8, f"забагато хибних спрацювань: точність {precision:.2f}"
        assert recall >= 0.8, f"детектор пропускає нестабільні тести: повнота {recall:.2f}"
    finally:
        db.close()


def test_flaky_scores_separate_stable_from_unstable():
    """Між нестабільними і звичайними тестами має бути помітний розрив оцінок."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.ml.flaky import compute_flaky_scores
    from app.models import CIProvider, Project

    db = SessionLocal()
    try:
        project = db.scalars(
            select(Project).where(Project.ci_provider == CIProvider.SYNTHETIC)
        ).first()
        if project is None:
            pytest.skip("немає синтетичного стенда")

        v = compute_flaky_scores(db, project.id)
        if len(v) < 10:
            pytest.skip("замало даних")

        flagged = [x.score for x in v if x.should_quarantine]
        rest = [x.score for x in v if not x.should_quarantine]
        if flagged and rest:
            assert min(flagged) > max(rest), "оцінки нестабільних і стабільних тестів перетинаються"
    finally:
        db.close()
