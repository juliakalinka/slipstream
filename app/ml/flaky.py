"""Задача 5: виявлення нестабільних (flaky) тестів.

Означення, яке використовується в роботі: тест нестабільний, якщо на ОДНОМУ
Й ТОМУ САМОМУ коміті він в одних запусках проходить, а в інших падає.
Це означення операційне: воно перевіряється безпосередньо за даними і не
потребує ручної розмітки.

Оцінка нестабільності складається з трьох складових:
  * flip_rate  - частка комітів, на яких результат тесту не був однаковим;
  * retry_rate - частка падінь, які «вилікувалися» повторним запуском;
  * duration_cv - коефіцієнт варіації тривалості (нестабільні тести часто
    мають розкид через гонки та очікування).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, Pipeline, TestCase, TestResult, TestStatus


@dataclass
class FlakyVerdict:
    test_case_id: int
    suite: str
    name: str
    score: float
    flip_rate: float
    retry_rate: float
    duration_cv: float
    runs: int
    distinct_commits: int
    should_quarantine: bool
    evidence: dict


def compute_flaky_scores(
    db: Session,
    project_id: int,
    min_runs: int | None = None,
    threshold: float | None = None,
) -> list[FlakyVerdict]:
    min_runs = min_runs or settings.min_runs_for_flaky
    threshold = threshold if threshold is not None else settings.flaky_threshold

    rows = db.execute(
        select(
            TestResult.test_case_id,
            Pipeline.commit_sha,
            TestResult.status,
            TestResult.duration_ms,
            TestResult.retries,
        )
        .join(Job, TestResult.job_id == Job.id)
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project_id)
    ).all()

    # групування: тест -> коміт -> список статусів
    per_test: dict[int, dict[str, list[str]]] = {}
    durations: dict[int, list[float]] = {}
    retries: dict[int, list[int]] = {}
    for tc_id, sha, status, dur, retr in rows:
        st = status.value if hasattr(status, "value") else str(status)
        per_test.setdefault(tc_id, {}).setdefault(sha, []).append(st)
        durations.setdefault(tc_id, []).append(float(dur))
        retries.setdefault(tc_id, []).append(int(retr or 0))

    names = {
        tc.id: (tc.suite, tc.name)
        for tc in db.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
    }

    verdicts: list[FlakyVerdict] = []
    for tc_id, by_commit in per_test.items():
        runs = sum(len(v) for v in by_commit.values())
        if runs < min_runs:
            continue

        # Переворот результату фіксується двома способами:
        #  1) той самий коміт перезапускався і дав різні результати;
        #  2) тест упав і пройшов після повтору в межах одного запуску -
        #     це за означенням різний результат на тому самому коді.
        repeated = {s: v for s, v in by_commit.items() if len(v) > 1}
        flipped = sum(1 for v in repeated.values() if len(set(v)) > 1)

        r = retries.get(tc_id, [])
        retry_cured = sum(1 for x in r if x > 0)
        retry_rate = retry_cured / len(r) if r else 0.0

        denom = len(repeated) + retry_cured
        flip_rate = (flipped + retry_cured) / denom if denom else 0.0
        flipped_total = flipped + retry_cured

        d = durations.get(tc_id, [])
        mean_d = statistics.fmean(d) if d else 0.0
        cv = (statistics.pstdev(d) / mean_d) if len(d) > 1 and mean_d else 0.0

        score = min(1.0, 0.60 * flip_rate + 0.30 * retry_rate + 0.10 * min(cv, 1.0))
        suite, name = names.get(tc_id, ("?", f"test#{tc_id}"))
        verdicts.append(
            FlakyVerdict(
                test_case_id=tc_id,
                suite=suite,
                name=name,
                score=round(score, 4),
                flip_rate=round(flip_rate, 4),
                retry_rate=round(retry_rate, 4),
                duration_cv=round(cv, 4),
                runs=runs,
                distinct_commits=len(by_commit),
                should_quarantine=score >= threshold and flipped_total >= 2,
                evidence={
                    "commits_with_repeats": len(repeated),
                    "commits_with_flip": flipped,
                    "retry_cured": retry_cured,
                    "mean_duration_ms": round(mean_d, 1),
                },
            )
        )

    verdicts.sort(key=lambda v: v.score, reverse=True)
    return verdicts


def persist_scores(db: Session, verdicts: list[FlakyVerdict], auto_quarantine: bool = False) -> int:
    """Записати оцінки в таблицю test_cases. Карантин за замовчуванням
    НЕ вмикається автоматично: рішення ухвалює DevOps-інженер через рекомендацію."""
    changed = 0
    for v in verdicts:
        tc = db.get(TestCase, v.test_case_id)
        if tc is None:
            continue
        tc.flaky_score = v.score
        if auto_quarantine and v.should_quarantine and not tc.quarantined:
            tc.quarantined = True
            changed += 1
    db.commit()
    return changed
