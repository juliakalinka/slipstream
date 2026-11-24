"""Підбір розміру вікна живих тестів.

Набір тестів проєкту змінюється з часом, тому кандидатами відбору мають бути
лише ті тести, які виконувалися в недавніх прогонах. Розмір цього вікна -
компроміс:

  * замале вікно відкидає тести, що виконуються не в кожному складанні
    (різні модулі, умовні набори, матриця платформ);
  * завелике повертає давно видалені тести, і бюджет часу витрачається на
    те, чого в складанні не буде.

Сценарій відтворює контрольну частину історії для кількох значень вікна і
показує, як змінюються частка виявлених дефектів і економія часу. Саме так
обрано значення ACTIVE_WINDOW_BUILDS за замовчуванням.

Важливо: сценарій НЕ перенавчає моделі, тому порівнюються лише наслідки
розміру вікна, а не випадкові відмінності навчання.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import app.services.plan_builder as pb  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.ml.training import _ordered_pipelines  # noqa: E402
from app.models import (  # noqa: E402
    Job, PlanMode, Project, TestResult, TestStatus,
)


def failed_tests(db, pipeline_id: int) -> set[int]:
    return {
        r[0] for r in db.execute(
            select(TestResult.test_case_id)
            .join(Job, TestResult.job_id == Job.id)
            .where(Job.pipeline_id == pipeline_id)
            .where(TestResult.status == TestStatus.FAILED)
        ).all()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Підбір розміру вікна живих тестів")
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--windows", default="1,3,5,10,20,30,60,200")
    ap.add_argument("--train-ratio", type=float, default=0.70)
    ap.add_argument("--limit", type=int, default=0, help="обмежити контрольну частину")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        raise SystemExit("проєкт не знайдено")

    rows = _ordered_pipelines(db, project.id)
    holdout = rows[int(len(rows) * args.train_ratio):]
    if args.limit:
        holdout = holdout[: args.limit]

    saved_mode = project.plan_mode
    project.plan_mode = PlanMode.ACTIVE
    db.commit()

    print("=" * 74)
    print(f"ПІДБІР ВІКНА ЖИВИХ ТЕСТІВ — {project.name}")
    print("=" * 74)
    print(f"Контрольна частина: {len(holdout)} конвеєрів")
    print()
    print(f"{'вікно':>7}{'кандидатів':>13}{'відібрано':>12}"
          f"{'виявлено':>12}{'економія':>11}")
    print("-" * 74)

    original = pb.ACTIVE_WINDOW_BUILDS
    try:
        for w in [int(x) for x in args.windows.split(",")]:
            pb.ACTIVE_WINDOW_BUILDS = w
            cand, sel, savings = [], [], []
            n_failed = n_caught = 0

            for pipeline, commit in holdout:
                started = pipeline.started_at
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                plan = pb.build_plan(db, project, commit, ref=pipeline.ref, cutoff=started)
                chosen = {t for j in plan.jobs for t in j.test_ids}

                cand.append(plan.total_tests)
                sel.append(plan.selected_tests)
                ratio = (plan.est_duration_s / plan.baseline_duration_s
                         if plan.baseline_duration_s else 1.0)
                savings.append(1 - ratio)

                actual = failed_tests(db, pipeline.id)
                if actual:
                    n_failed += 1
                    if plan.mode in ("full", "sharded") or (chosen & actual):
                        n_caught += 1

            det = n_caught / n_failed * 100 if n_failed else 100.0
            print(f"{w:>7}{statistics.median(cand):>13.0f}{statistics.median(sel):>12.0f}"
                  f"{det:>11.1f}%{statistics.fmean(savings) * 100:>10.1f}%")
    finally:
        pb.ACTIVE_WINDOW_BUILDS = original
        project.plan_mode = saved_mode
        db.commit()
        db.close()

    print("-" * 74)
    print("Виявлення і економія рухаються в різні боки: вужче вікно лишає менше")
    print("кандидатів, тому потрібні тести проходять відсікання частіше.")


if __name__ == "__main__":
    main()
