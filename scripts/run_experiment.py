"""Експеримент «базовий конвеєр проти оптимізованого».

Це головний кількісний результат роботи (підрозділ 4.7 пояснювальної записки).

МЕТОДИКА, яка робить порівняння чесним:
  1. Історію ділимо за ЧАСОМ: перші train_ratio відсотків - навчальна частина,
     решта - контрольна. Моделі навчаються ЛИШЕ на навчальній частині.
  2. Для кожного конвеєра з контрольної частини відтворюємо рішення системи
     так, ніби вона працювала в той момент: історія обрізається моментом
     старту цього конвеєра.
  3. Порівнюємо два показники:
       * скільки часу зекономлено;
       * чи виявлено дефект, тобто чи потрапив до відібраного набору хоча б
         один тест, який насправді впав.

Другий показник критичний: економія часу нічого не варта, якщо система
перестає ловити помилки. У звіті він називається часткою виявлених дефектів.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.ml.test_selector import apfd  # noqa: E402
from app.ml.training import _ordered_pipelines, train_all  # noqa: E402
from app.models import (  # noqa: E402
    CIProvider, Commit, Job, Pipeline, PlanMode, Project, RunStatus, TestResult,
    TestStatus,
)
from app.services.plan_builder import ACTIVE_WINDOW_BUILDS, build_plan  # noqa: E402


def actual_failed_tests(db, pipeline_id: int) -> set[int]:
    rows = db.execute(
        select(TestResult.test_case_id)
        .join(Job, TestResult.job_id == Job.id)
        .where(Job.pipeline_id == pipeline_id)
        .where(TestResult.status == TestStatus.FAILED)
    ).all()
    return {r[0] for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser(description="Експеримент: базовий конвеєр проти оптимізованого")
    ap.add_argument("--project", type=int, default=1)
    ap.add_argument("--train-ratio", type=float, default=0.70)
    ap.add_argument("--retrain", action="store_true", help="перенавчити моделі лише на навчальній частині")
    ap.add_argument("--limit", type=int, default=0, help="обмежити кількість конвеєрів контрольної частини")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        print("Проєкт не знайдено. Спершу виконайте scripts/seed_synthetic.py")
        return

    rows = _ordered_pipelines(db, project.id)
    if len(rows) < 60:
        print("Замало конвеєрів для експерименту")
        return

    cut = int(len(rows) * args.train_ratio)
    cutoff = rows[cut][0].started_at
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    print("=" * 74)
    print("ЕКСПЕРИМЕНТ: базовий конвеєр проти оптимізованого")
    print("=" * 74)
    print(f"Проєкт:              {project.name}")
    print(f"Усього конвеєрів:    {len(rows)}")
    print(f"Навчальна частина:   {cut} (до {cutoff:%Y-%m-%d %H:%M})")
    print(f"Контрольна частина:  {len(rows) - cut}")

    if args.retrain:
        print("\nПеренавчання моделей лише на навчальній частині...")
        rep = train_all(db, project.id, project.default_branch, cutoff=cutoff)
        for name, res in rep.models.items():
            if "metrics" in res:
                m = res["metrics"]
                key = next((k for k in ("roc_auc", "r2", "flagged_ratio") if k in m), None)
                print(f"   {name}: {key}={m.get(key)}" if key else f"   {name}: навчено")

    holdout = rows[cut:]
    if args.limit:
        holdout = holdout[: args.limit]

    # прапорець режиму не має заважати експерименту
    saved_mode = project.plan_mode
    project.plan_mode = PlanMode.ACTIVE
    db.commit()

    savings, base_times, opt_times = [], [], []
    sel_counts, total_counts, apfds = [], [], []
    n_failed = n_caught = n_missed = 0
    n_skipped = n_full = n_opt = 0
    sel_failed = sel_caught = 0   # лише прогони з реальним відбором тестів
    sel_savings = []
    missed_examples = []

    print(f"\nВідтворення {len(holdout)} конвеєрів контрольної частини...")
    for i, (pipeline, commit) in enumerate(holdout):
        started = pipeline.started_at
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        plan = build_plan(db, project, commit, ref=pipeline.ref, cutoff=started)
        chosen = {t for j in plan.jobs for t in j.test_ids}

        if plan.mode == "skip":
            n_skipped += 1
        elif plan.mode == "selective":
            n_opt += 1
        else:
            n_full += 1

        base_s = float(pipeline.duration_s)
        opt_s = plan.est_duration_s
        # поправка масштабу: прогноз моделі й фактична тривалість вимірюються
        # в одній шкалі лише приблизно, тому економію рахуємо у відносних величинах
        ratio = plan.est_duration_s / plan.baseline_duration_s if plan.baseline_duration_s else 1.0
        opt_equiv = base_s * ratio

        base_times.append(base_s)
        opt_times.append(opt_equiv)
        savings.append(1 - ratio)
        if plan.mode == "selective":
            sel_savings.append(1 - ratio)
        sel_counts.append(plan.selected_tests)
        total_counts.append(plan.total_tests)

        failed = actual_failed_tests(db, pipeline.id)
        if failed:
            n_failed += 1
            if plan.mode == "skip":
                n_missed += 1
                missed_examples.append((commit.sha[:8], pipeline.ref, "пропуск конвеєра", len(failed)))
            elif chosen & failed:
                n_caught += 1
                if plan.mode == "selective":
                    sel_failed += 1
                    sel_caught += 1
            elif plan.mode in ("full", "sharded"):
                n_caught += 1
            else:
                n_missed += 1
                sel_failed += 1
                missed_examples.append((commit.sha[:8], pipeline.ref, f"відібрано {plan.selected_tests}", len(failed)))

            order = [t for j in sorted(plan.jobs, key=lambda x: x.order) for t in j.test_ids]
            if order:
                apfds.append(apfd(order, failed))

        if (i + 1) % 40 == 0:
            print(f"   оброблено {i + 1}/{len(holdout)}")

    project.plan_mode = saved_mode
    db.commit()

    mean_base = statistics.fmean(base_times)
    mean_opt = statistics.fmean(opt_times)
    mean_sav = statistics.fmean(savings)
    detection = n_caught / n_failed if n_failed else 1.0

    print("\n" + "=" * 74)
    print("РЕЗУЛЬТАТИ")
    print("=" * 74)
    print(f"{'Показник':<46}{'Базовий':>13}{'Оптимізований':>15}")
    print("-" * 74)
    print(f"{'Середня тривалість конвеєра, с':<46}{mean_base:>13.0f}{mean_opt:>15.0f}")
    print(f"{'Середня тривалість конвеєра, хв':<46}{mean_base/60:>13.1f}{mean_opt/60:>15.1f}")
    print(f"{'Медіанна тривалість, с':<46}{statistics.median(base_times):>13.0f}{statistics.median(opt_times):>15.0f}")
    print(f"{'Середня кількість запущених тестів':<46}{statistics.fmean(total_counts):>13.0f}{statistics.fmean(sel_counts):>15.0f}")
    print("-" * 74)
    print(f"{'Скорочення часу виконання':<46}{'':>13}{mean_sav:>14.1%}")
    print(f"{'Зекономлено на контрольній частині, год':<46}{'':>13}{(sum(base_times)-sum(opt_times))/3600:>14.1f}")
    print(f"{'Частка виявлених дефектів (усі прогони)':<46}{'':>13}{detection:>14.1%}")
    if sel_failed:
        print(f"{'Те саме лише для вибіркових прогонів':<46}{'':>13}{sel_caught/sel_failed:>14.1%}")
    if sel_savings:
        print(f"{'Скорочення часу на вибіркових прогонах':<46}{'':>13}{statistics.fmean(sel_savings):>14.1%}")
    if apfds:
        print(f"{'APFD (якість упорядкування тестів)':<46}{'':>13}{statistics.fmean(apfds):>14.3f}")
    print("-" * 74)
    print(f"Конвеєрів із дефектами: {n_failed}, виявлено {n_caught}, пропущено {n_missed}")
    print(f"Рішення системи: вибірковий прогін {n_opt}, повний набір {n_full}, пропуск {n_skipped}")

    if missed_examples:
        print("\nПропущені дефекти (перші 5):")
        for sha, ref, why, k in missed_examples[:5]:
            print(f"   коміт {sha} гілка {ref}: {why}, насправді впало {k} тестів")

    if project.ci_provider == CIProvider.SYNTHETIC:
        print("\nЗастереження: результати отримано на СИНТЕТИЧНІЙ історії.")
        print("Генератор закладає ті самі залежності, які модель потім відновлює,")
        print("тому для розділу 4 записки ці числа є допоміжними. Основні вимірювання")
        print("робіть на датасеті RTPTorrent (scripts/import_rtptorrent.py) або на")
        print("власному стенді GitLab.")
    else:
        print("\nДані реальні. Застереження, які треба зазначити в записці:")
        print("  * набір тестів проєкту змінюється, тому кандидатами є лише ті,")
        print(f"    що виконувалися за останні {ACTIVE_WINDOW_BUILDS} прогонів;")
        print("  * якщо в історії немає назв гілок, запобіжник захищеної гілки")
        print("    не діє, і оцінюється сам метод відбору, а не політика.")
    db.close()


if __name__ == "__main__":
    main()
