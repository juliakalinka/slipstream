"""Формування рекомендацій щодо оптимізації конвеєра.

Рекомендація - це керуючий вплив, запропонований системою, але затверджений
людиною. Кожна рекомендація містить доказову базу (evidence) і оцінку
очікуваного виграшу в секундах, щоб DevOps-інженер міг зважити користь.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.ml.flaky import compute_flaky_scores
from app.ml.sharding import suggest_shard_count
from app.models import (
    Policy, PolicyType, Project, RecoStatus, RecoType, Recommendation, TestCase,
)
from app.services.metrics import pipeline_metrics, slowest_jobs
from app.services.notification import notify_recommendation


def _exists(db: Session, project_id: int, rtype: RecoType, title: str) -> bool:
    return db.scalars(
        select(Recommendation)
        .where(Recommendation.project_id == project_id)
        .where(Recommendation.type == rtype)
        .where(Recommendation.title == title)
        .where(Recommendation.status == RecoStatus.NEW)
    ).first() is not None



def _flaky_reason(v) -> str:
    """Пояснення для людини: наводимо лише ті свідчення, які справді є.

    Формулювання будується за фактом, інакше в тексті з'являються звороти
    на кшталт «different results 0 times», які виглядають як помилка.
    """
    flips = int(v.evidence.get("commits_with_flip", 0) or 0)
    cured = int(v.evidence.get("retry_cured", 0) or 0)
    parts = []
    if flips:
        parts.append(
            f"produced different results on identical code {flips} "
            f"time{'' if flips == 1 else 's'}"
        )
    if cured:
        parts.append(f"passed on retry {cured} time{'' if cured == 1 else 's'}")
    evidence = " and ".join(parts) if parts else "behaves inconsistently between runs"
    return (
        f"This test {evidence}. Instability score {v.score:.2f}. "
        f"It finds no defects, but it blocks the pipeline and triggers repeated runs."
    )

def generate(db: Session, project: Project) -> list[Recommendation]:
    """Переглянути стан проєкту і сформувати нові рекомендації."""
    created: list[Recommendation] = []

    # --- 1. нестабільні тести у кандидати на карантин
    for v in compute_flaky_scores(db, project.id):
        if not v.should_quarantine:
            continue
        tc = db.get(TestCase, v.test_case_id)
        if tc is None or tc.quarantined:
            continue
        title = f"Quarantine flaky test {v.suite}/{v.name}"
        if _exists(db, project.id, RecoType.QUARANTINE_FLAKY, title):
            continue
        # виграш: тест перестає спричиняти повторні прогони конвеєра
        gain = int((tc.avg_duration_ms / 1000.0) * max(v.evidence.get("retry_cured", 0), 1))
        r = Recommendation(
            project_id=project.id, type=RecoType.QUARANTINE_FLAKY, title=title,
            description=_flaky_reason(v),
            evidence={"flaky_score": v.score, "flip_rate": v.flip_rate,
                      "retry_rate": v.retry_rate, "runs": v.runs, **v.evidence},
            expected_gain_s=gain,
        )
        db.add(r)
        created.append(r)

    # --- 2. збільшення кількості шардів
    m = pipeline_metrics(db, project.id, days=30)
    if m.total >= 20:
        sharding = db.scalars(
            select(Policy).where(Policy.project_id == project.id,
                                 Policy.type == PolicyType.SHARDING)
        ).first()
        cur = int((sharding.params or {}).get("max_shards", settings.default_shards)) if sharding else settings.default_shards
        target = int((sharding.params or {}).get("target_s", 90)) if sharding else 90
        need = suggest_shard_count(m.mean_duration_s, target, max_shards=16)
        if need > cur:
            title = f"Increase shard count from {cur} to {need}"
            if not _exists(db, project.id, RecoType.INCREASE_SHARDS, title):
                gain = int(m.mean_duration_s * (1 - cur / need) * 0.6)
                r = Recommendation(
                    project_id=project.id, type=RecoType.INCREASE_SHARDS, title=title,
                    description=(
                        f"Mean pipeline duration {m.mean_duration_s:.0f}s exceeds the "
                        f"{target}s target per shard. More parallelism shortens the critical path."
                    ),
                    evidence={"mean_duration_s": m.mean_duration_s, "current_shards": cur,
                              "suggested_shards": need, "pipelines": m.total},
                    expected_gain_s=max(gain, 0),
                )
                db.add(r)
                created.append(r)

    # --- 3. найповільніші задачі як кандидати на кешування або поділ
    for job in slowest_jobs(db, project.id, days=30, limit=3):
        if job["mean_duration_s"] < 120 or job["runs"] < 10:
            continue
        title = f"Split or speed up job {job['name']}"
        if _exists(db, project.id, RecoType.SPLIT_SLOW_JOB, title):
            continue
        r = Recommendation(
            project_id=project.id, type=RecoType.SPLIT_SLOW_JOB, title=title,
            description=(
                f"This job takes {job['mean_duration_s']:.0f}s on average "
                f"(peak {job['max_duration_s']}s) and defines the pipeline critical path. "
                f"Split it into parallel parts or enable dependency caching."
            ),
            evidence=job,
            expected_gain_s=int(job["mean_duration_s"] * 0.35),
        )
        db.add(r)
        created.append(r)

    db.commit()
    for r in created:
        db.refresh(r)
        notify_recommendation(r, project.name)
    return created


def apply(db: Session, reco: Recommendation, user_id: int | None) -> dict:
    """Застосувати рекомендацію: змінити стан системи, а не лише позначку."""
    result: dict = {"applied": [], "warnings": []}

    if reco.type == RecoType.QUARANTINE_FLAKY:
        tc_id = None
        title_parts = reco.title.rsplit(" ", 1)
        if title_parts:
            suite_name = title_parts[-1]
            if "/" in suite_name:
                suite, name = suite_name.split("/", 1)
                tc = db.scalars(
                    select(TestCase).where(
                        TestCase.project_id == reco.project_id,
                        TestCase.suite == suite, TestCase.name == name,
                    )
                ).first()
                if tc:
                    tc.quarantined = True
                    tc_id = tc.id
                    result["applied"].append(f"test {suite}/{name} moved to quarantine")
        if tc_id is None:
            result["warnings"].append("test not found; only the recommendation status changed")

    elif reco.type == RecoType.INCREASE_SHARDS:
        need = int((reco.evidence or {}).get("suggested_shards", settings.default_shards))
        pol = db.scalars(
            select(Policy).where(Policy.project_id == reco.project_id,
                                 Policy.type == PolicyType.SHARDING)
        ).first()
        if pol:
            params = dict(pol.params or {})
            params["max_shards"] = need
            pol.params = params
            result["applied"].append(f"shard count raised to {need}")
        else:
            db.add(Policy(project_id=reco.project_id, name="Shard distribution",
                          type=PolicyType.SHARDING, params={"max_shards": need, "target_s": 90}))
            result["applied"].append(f"sharding policy created with {need} shards")

    else:
        result["warnings"].append("advisory only; nothing was changed automatically")

    reco.status = RecoStatus.APPLIED
    reco.applied_by = user_id
    reco.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return result


def reject(db: Session, reco: Recommendation, user_id: int | None) -> None:
    reco.status = RecoStatus.REJECTED
    reco.applied_by = user_id
    reco.resolved_at = datetime.now(timezone.utc)
    db.commit()
