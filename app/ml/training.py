"""Конвеєр навчання моделей.

Центральна ідея всього модуля: історія проходиться СТРОГО в хронологічному
порядку, і ознаки для конвеєра, що стартував у момент t, рахуються за станом
історії до моменту t. Завдяки цьому навчальна вибірка не містить інформації
з майбутнього, а оцінка якості на відкладеній у часі частині є чесною.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ml import anomaly, duration_model, failure_model, registry
from app.ml.features import (
    DURATION_FEATURES, FAILURE_FEATURES, HistoryStats, build_commit_features,
    build_job_features, features_to_vector, module_of,
)
from app.ml.flaky import compute_flaky_scores, persist_scores
from app.ml.test_selector import RANK_FEATURES, SuiteStats, TestStats, build_rank_features
from app.models import (
    Commit, Job, ModelType, Pipeline, RunStatus, TestCase, TestResult, TestStatus,
)


@dataclass
class TrainingReport:
    project_id: int
    models: dict[str, dict]
    flaky_found: int
    pipelines_used: int
    notes: list[str]


def _ordered_pipelines(
    db: Session, project_id: int, cutoff: datetime | None = None
) -> list[tuple[Pipeline, Commit]]:
    """Конвеєри проєкту в хронологічному порядку, опційно лише до моменту cutoff."""
    stmt = (
        select(Pipeline, Commit)
        .join(Commit, Pipeline.commit_sha == Commit.sha)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.status.in_([RunStatus.SUCCESS, RunStatus.FAILED]))
        .order_by(Pipeline.started_at)
    )
    if cutoff is not None:
        stmt = stmt.where(Pipeline.started_at < cutoff)
    return list(db.execute(stmt).all())


# ------------------------------------------------------------------ модель збою
def train_failure(db: Session, project_id: int, default_branch: str = "main", cutoff: datetime | None = None) -> dict:
    rows = _ordered_pipelines(db, project_id, cutoff)
    hist = HistoryStats()
    X, y = [], []

    for pipeline, commit in rows:
        feats = build_commit_features(commit, pipeline.ref, hist, default_branch)
        X.append(features_to_vector(feats, FAILURE_FEATURES))
        y.append(1 if pipeline.status == RunStatus.FAILED else 0)
        hist.observe(commit, pipeline)  # спостереження ПІСЛЯ формування ознак

    Xa, ya = np.asarray(X, dtype=float), np.asarray(y, dtype=int)
    model, metrics = failure_model.train(Xa, ya)
    metrics["feature_importance"] = dict(
        list(failure_model.feature_importance(model, FAILURE_FEATURES).items())[:10]
    )
    mv = registry.save(
        db, project_id, ModelType.FAILURE, model, FAILURE_FEATURES, metrics,
        failure_model.algorithm_name(), len(ya),
    )
    return {"version": mv.version, "metrics": metrics}


# ------------------------------------------------------------- модель тривалості
def train_duration(db: Session, project_id: int, cutoff: datetime | None = None) -> dict:
    rows = _ordered_pipelines(db, project_id, cutoff)
    job_hist: dict[str, list[float]] = defaultdict(list)

    tests_per_suite = dict(
        db.execute(
            select(TestCase.suite, TestCase.id).where(TestCase.project_id == project_id)
        ).all()
    )
    suite_counts: dict[str, int] = defaultdict(int)
    for suite, _ in db.execute(
        select(TestCase.suite, TestCase.id).where(TestCase.project_id == project_id)
    ).all():
        suite_counts[suite] += 1

    X, y = [], []
    for pipeline, commit in rows:
        jobs = db.scalars(select(Job).where(Job.pipeline_id == pipeline.id)).all()
        for job in jobs:
            if job.duration_s <= 0:
                continue
            base = job.name.split(" ")[0]
            n_tests = suite_counts.get(job.name.replace("test:", ""), 0)
            feats = build_job_features(
                base, job.stage, n_tests, commit, job_hist,
                cache_hit=job.cache_hit, is_sharded=job.shard_index is not None,
            )
            X.append([feats[k] for k in DURATION_FEATURES])
            y.append(float(job.duration_s))
            job_hist[base].append(float(job.duration_s))
            if len(job_hist[base]) > 60:
                job_hist[base].pop(0)

    Xa, ya = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    model, metrics = duration_model.train(Xa, ya)
    mv = registry.save(
        db, project_id, ModelType.DURATION, model, DURATION_FEATURES, metrics,
        duration_model.algorithm_name(), len(ya),
    )
    return {"version": mv.version, "metrics": metrics}


# ------------------------------------------------------- модель ранжування тестів
def train_test_ranker(db: Session, project_id: int, max_pipelines: int = 600, cutoff: datetime | None = None) -> dict:
    """Навчання моделі, яка оцінює ймовірність падіння тесту на цій зміні.

    Вибірка сильно незбалансована: падає одиниці тестів зі сотень. Тому
    застосовано зважування класів, а якість оцінюється за PR-AUC, а не за
    часткою правильних відповідей, яка тут неінформативна.
    """
    rows = _ordered_pipelines(db, project_id, cutoff)[-max_pipelines:]
    tests = db.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
    by_id = {t.id: t for t in tests}

    # інкрементальна історія по кожному тесту
    stats: dict[int, TestStats] = {
        t.id: TestStats(
            test_id=t.id, suite=t.suite, name=t.name, file_path=t.file_path,
            avg_duration_ms=t.avg_duration_ms or 1000.0,
        )
        for t in tests
    }
    suite_stats = SuiteStats()

    X, y = [], []
    for pipeline, commit in rows:
        results = db.execute(
            select(TestResult.test_case_id, TestResult.status, TestResult.duration_ms)
            .join(Job, TestResult.job_id == Job.id)
            .where(Job.pipeline_id == pipeline.id)
        ).all()
        if not results:
            continue

        paths = commit.changed_paths or []
        now = pipeline.started_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # ознаки рахуємо ДО оновлення історії цим конвеєром
        for tc_id, status, dur in results:
            st = stats.get(tc_id)
            if st is None:
                continue
            feats = build_rank_features(st, paths, now, suite_stats)
            X.append([feats[k] for k in RANK_FEATURES])
            y.append(1 if status == TestStatus.FAILED else 0)

        # тепер оновлюємо історію
        modules = {module_of(p) for p in paths}
        suite_failed: dict[str, bool] = {}
        for tc_id, status, dur in results:
            st = stats.get(tc_id)
            if st is None:
                continue
            failed = status == TestStatus.FAILED
            st.runs += 1
            suite_failed[st.suite] = suite_failed.get(st.suite, False) or failed
            if failed:
                st.failures += 1
                st.last_failed_at = now
                for m in modules:
                    st.module_failures[m] = st.module_failures.get(m, 0) + 1
        for suite, failed in suite_failed.items():
            suite_stats.observe(suite, modules, failed)

    Xa, ya = np.asarray(X, dtype=float), np.asarray(y, dtype=int)
    if len(ya) < 200 or ya.sum() < 10:
        return {"skipped": True, "reason": f"замало падінь тестів для навчання: {int(ya.sum())}"}

    from sklearn.metrics import average_precision_score, roc_auc_score

    cut = int(len(ya) * 0.75)
    X_tr, X_te, y_tr, y_te = Xa[:cut], Xa[cut:], ya[:cut], ya[cut:]

    try:
        from lightgbm import LGBMClassifier
        est = LGBMClassifier(
            n_estimators=250, learning_rate=0.05, num_leaves=15,
            class_weight="balanced", verbose=-1, random_state=42,
        )
        algo = "LightGBM Ranker (binary)"
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier
        est = HistGradientBoostingClassifier(max_iter=250, random_state=42)
        algo = "HistGradientBoosting"

    est.fit(X_tr, y_tr)
    metrics: dict = {"train_samples": int(len(y_tr)), "positives": int(ya.sum())}
    if len(np.unique(y_te)) == 2:
        proba = est.predict_proba(X_te)[:, 1]
        metrics |= {
            "roc_auc": round(float(roc_auc_score(y_te, proba)), 4),
            "pr_auc": round(float(average_precision_score(y_te, proba)), 4),
            "base_rate": round(float(y_te.mean()), 5),
        }
        if metrics["base_rate"]:
            metrics["lift_over_random"] = round(metrics["pr_auc"] / metrics["base_rate"], 1)

    mv = registry.save(
        db, project_id, ModelType.TEST_RANK, est, RANK_FEATURES, metrics, algo, len(ya)
    )
    return {"version": mv.version, "metrics": metrics}


# ---------------------------------------------------------- детектор аномалій
def train_anomaly(db: Session, project_id: int, cutoff: datetime | None = None) -> dict:
    rows = _ordered_pipelines(db, project_id, cutoff)
    X = []
    for pipeline, _ in rows:
        jobs = db.scalars(select(Job).where(Job.pipeline_id == pipeline.id)).all()
        n_failed = sum(1 for j in jobs if j.status == RunStatus.FAILED)
        n_tests = db.scalar(
            select(TestResult.id).join(Job, TestResult.job_id == Job.id)
            .where(Job.pipeline_id == pipeline.id).limit(1)
        )
        X.append([
            float(pipeline.duration_s), float(pipeline.queue_time_s), float(len(jobs)),
            (n_failed / len(jobs)) if jobs else 0.0, float(1 if n_tests else 0),
        ])
    if len(X) < 50:
        return {"skipped": True, "reason": "замало конвеєрів для детектора аномалій"}

    Xa = np.asarray(X, dtype=float)
    det = anomaly.train_detector(Xa)
    flagged = int((det.predict(Xa) == -1).sum())
    metrics = {
        "train_samples": len(Xa),
        "flagged": flagged,
        "flagged_ratio": round(flagged / len(Xa), 4),
    }
    mv = registry.save(
        db, project_id, ModelType.ANOMALY, det, anomaly.ANOMALY_FEATURES, metrics,
        "IsolationForest", len(Xa),
    )
    return {"version": mv.version, "metrics": metrics}


# ------------------------------------------------------------------ повний прохід
def train_all(
    db: Session, project_id: int, default_branch: str = "main",
    cutoff: datetime | None = None,
) -> TrainingReport:
    notes: list[str] = []
    models: dict[str, dict] = {}

    rows = _ordered_pipelines(db, project_id, cutoff)
    for name, fn in [
        ("failure", lambda: train_failure(db, project_id, default_branch, cutoff)),
        ("duration", lambda: train_duration(db, project_id, cutoff)),
        ("test_rank", lambda: train_test_ranker(db, project_id, cutoff=cutoff)),
        ("anomaly", lambda: train_anomaly(db, project_id, cutoff)),
    ]:
        try:
            models[name] = fn()
        except Exception as exc:  # навчання однієї моделі не має валити решту
            models[name] = {"error": str(exc)}
            notes.append(f"{name}: {exc}")

    verdicts = compute_flaky_scores(db, project_id)
    persist_scores(db, verdicts, auto_quarantine=False)
    flaky = sum(1 for v in verdicts if v.should_quarantine)

    registry.clear_cache()
    return TrainingReport(
        project_id=project_id, models=models, flaky_found=flaky,
        pipelines_used=len(rows), notes=notes,
    )
