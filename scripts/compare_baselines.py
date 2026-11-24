"""Порівняння методу впорядкування тестів із базовими методами.

НАВІЩО. Саме по собі число «APFD = 0,49» нічого не означає: невідомо, добре це
чи погано. Значення має лише порівняння з опублікованими базовими методами на
тих самих даних. Цей сценарій рахує APFD для чотирьох способів упорядкування
на одній і тій самій історії:

    original   порядок, у якому тести справді виконувалися (без упорядкування);
               у статті RTPTorrent це «Build APFD», середнє по датасету 25,9 %
    random     випадковий порядок, усереднений за кількома спробами
    dfe        Demonstrated Fault Effectiveness, базовий метод зі статті:
               P_t(n) = a*F_t(n) + (1-a)*P_t(n-1), P_t(0)=0, a=0,8;
               середнє по датасету 81,1 %
    ours       запропонований метод: ознаки спорідненості «модуль - тест»
               плюс історія падінь, з навченою моделлю або евристикою

    Toni Mattis, Patrick Rein, Falco Dürsch, Robert Hirschfeld. RTPTorrent:
    An Open-source Dataset for Evaluating Regression Test Prioritization.
    MSR '20, табл. 3.

ЧЕСНІСТЬ ВИМІРЮВАННЯ. Прохід строго хронологічний. Для кожного конвеєра
порядок будується ЛИШЕ зі стану, накопиченого за попередніми конвеєрами, і
тільки після обчислення APFD стан оновлюється результатами цього конвеєра.
Тому жодна ознака не бачить майбутнього.

APFD визначено тільки для прогонів, де впав хоча б один тест; решта
пропускається, як і в статті.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.ml import registry  # noqa: E402
from app.ml.features import module_of  # noqa: E402
from app.ml.test_selector import (  # noqa: E402
    SuiteStats, TestRanker, TestStats, apfd, build_rank_features,
)
from app.models import (  # noqa: E402
    Commit, Job, ModelType, Pipeline, Project, TestCase, TestResult, TestStatus,
)

ALPHA = 0.8  # згладжування DFE, як у статті


def load_history(db, project_id: int):
    """Уся історія проєкту одним проходом: конвеєри, їхні коміти й результати."""
    pipes = db.execute(
        select(Pipeline.id, Pipeline.started_at, Pipeline.commit_sha)
        .where(Pipeline.project_id == project_id)
        .where(Pipeline.started_at.is_not(None))
        .order_by(Pipeline.started_at, Pipeline.id)
    ).all()

    paths = {
        sha: (cp or [])
        for sha, cp in db.execute(
            select(Commit.sha, Commit.changed_paths).where(Commit.project_id == project_id)
        ).all()
    }

    rows = db.execute(
        select(Job.pipeline_id, TestResult.test_case_id, TestResult.status, TestResult.id)
        .join(TestResult, TestResult.job_id == Job.id)
        .join(Pipeline, Job.pipeline_id == Pipeline.id)
        .where(Pipeline.project_id == project_id)
        .order_by(Job.pipeline_id, TestResult.id)
    ).all()

    # ДУБЛІКАТИ. Один коміт породжує кілька задач матриці, і той самий тестовий
    # клас виконується в кількох із них. У формулі APFD кожен тест має входити
    # РІВНО один раз, інакше позиції повторюються, сума перевищує n*m і
    # показник стає від'ємним. Тому лишаємо перше входження (порядок виконання)
    # і вважаємо тест таким, що впав, якщо він упав хоча б в одній задачі.
    by_pipe: dict[int, list[tuple[int, TestStatus]]] = {}
    pos: dict[int, dict[int, int]] = {}
    for pid, tc_id, status, _rid in rows:
        seen = by_pipe.setdefault(pid, [])
        where = pos.setdefault(pid, {})
        i = where.get(tc_id)
        if i is None:
            where[tc_id] = len(seen)
            seen.append((tc_id, status))
        elif status == TestStatus.FAILED:
            seen[i] = (tc_id, TestStatus.FAILED)

    cases = {
        c.id: c for c in
        db.scalars(select(TestCase).where(TestCase.project_id == project_id))
    }
    return pipes, paths, by_pipe, cases


def main() -> None:
    ap = argparse.ArgumentParser(description="Порівняння з базовими методами впорядкування")
    ap.add_argument("--project", type=int, required=True)
    ap.add_argument("--warmup", type=float, default=0.30,
                    help="частка історії лише на накопичення стану, без вимірювання")
    ap.add_argument("--random-trials", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dump-jobs", default="",
                    help="файл, куди записати перелік виміряних складань "
                         "(щоб базові методи датасету рахувалися на тих самих)")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        raise SystemExit("проєкт не знайдено")

    pipes, paths, by_pipe, cases = load_history(db, project.id)
    usable = [p for p in pipes if by_pipe.get(p[0])]
    if len(usable) < 40:
        raise SystemExit(f"замало конвеєрів із результатами тестів: {len(usable)}")

    rank_mv = registry.active(db, project.id, ModelType.TEST_RANK)
    ranker = TestRanker()
    model_note = "евристика (модель ранжування не навчена)"
    if rank_mv is not None:
        est, names = registry.load(rank_mv)
        ranker = TestRanker(est, names)
        model_note = f"навчена модель {rank_mv.version}"

    print("=" * 74)
    print("ПОРІВНЯННЯ МЕТОДІВ УПОРЯДКУВАННЯ ТЕСТІВ")
    print("=" * 74)
    print(f"Проєкт:              {project.name}")
    print(f"Конвеєрів з тестами: {len(usable)}")
    print(f"Тестових класів:     {len(cases)}")
    print(f"Метод «ours»:        {model_note}")

    warm = int(len(usable) * args.warmup)
    print(f"Розігрів:            {warm} конвеєрів (стан накопичується, APFD не рахується)")

    # ---- стан, що накопичується строго з минулого
    runs: dict[int, int] = defaultdict(int)
    fails: dict[int, int] = defaultdict(int)
    last_fail: dict[int, datetime] = {}
    mod_fails: dict[int, dict[str, int]] = defaultdict(dict)
    suite_stats = SuiteStats()
    dfe: dict[int, float] = defaultdict(float)

    scores = {k: [] for k in ("original", "random", "dfe", "ours")}
    rnd = random.Random(args.seed)
    measured = 0
    measured_ids: list[str] = []

    for n, (pid, started, sha) in enumerate(usable):
        results = by_pipe[pid]
        order_original = [tc for tc, _ in results]           # як справді виконувалося
        failing = {tc for tc, st in results if st == TestStatus.FAILED}
        changed = paths.get(sha, [])
        if started and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)

        if n >= warm and failing and len(order_original) > 1:
            measured += 1
            measured_ids.append(pid)
            scores["original"].append(apfd(order_original, failing))

            rs = []
            for t in range(args.random_trials):
                shuffled = order_original[:]
                rnd.shuffle(shuffled)
                rs.append(apfd(shuffled, failing))
            scores["random"].append(statistics.fmean(rs))

            by_dfe = sorted(order_original, key=lambda tc: -dfe[tc])
            scores["dfe"].append(apfd(by_dfe, failing))

            feats, ids = [], []
            for tc in order_original:
                c = cases.get(tc)
                if c is None:
                    continue
                ts = TestStats(
                    test_id=tc, suite=c.suite, name=c.name, file_path=c.file_path,
                    avg_duration_ms=c.avg_duration_ms or 1000.0,
                    runs=runs[tc], failures=fails[tc],
                    flaky_score=0.0, last_failed_at=last_fail.get(tc),
                )
                ts.module_failures = dict(mod_fails.get(tc, {}))
                feats.append(build_rank_features(ts, changed, now=started, suite_stats=suite_stats))
                ids.append(tc)
            if ids:
                sc = ranker.score_many(feats)
                by_ours = [tc for _, tc in sorted(zip(sc, ids), key=lambda x: -x[0])]
                scores["ours"].append(apfd(by_ours, failing))

        # ---- аж тепер оновлюємо стан результатами цього конвеєра
        mods = {module_of(p) for p in changed}
        for tc, st in results:
            runs[tc] += 1
            failed = st == TestStatus.FAILED
            dfe[tc] = ALPHA * (1.0 if failed else 0.0) + (1 - ALPHA) * dfe[tc]
            if failed:
                fails[tc] += 1
                last_fail[tc] = started
                mf = mod_fails[tc]
                for m in mods:
                    mf[m] = mf.get(m, 0) + 1
            c = cases.get(tc)
            if c is not None:
                suite_stats.observe(c.suite, mods, failed)

    if not measured:
        raise SystemExit("жодного прогону з падінням у вимірюваній частині")

    if args.dump_jobs:
        ext = {
            p_id: e for p_id, e in db.execute(
                select(Pipeline.id, Pipeline.external_id)
                .where(Pipeline.project_id == project.id)
            ).all()
        }
        Path(args.dump_jobs).write_text(
            "\n".join(str(ext[i]) for i in measured_ids if i in ext), encoding="utf-8")
        print(f"Перелік виміряних складань записано: {args.dump_jobs}")

    print(f"Прогонів із падінням: {measured}")
    print()
    print(f"{'Метод':<34}{'APFD, середнє':>16}{'медіана':>12}")
    print("-" * 74)
    labels = {
        "random": "Випадковий порядок",
        "original": "Наявний порядок (без упорядкування)",
        "dfe": f"DFE, базовий зі статті (a={ALPHA})",
        "ours": "Запропонований метод",
    }
    for key in ("random", "original", "dfe", "ours"):
        v = scores[key]
        if not v:
            continue
        print(f"{labels[key]:<34}{statistics.fmean(v) * 100:>15.1f}%{statistics.median(v) * 100:>11.1f}%")
    print("-" * 74)

    if scores["ours"] and scores["original"]:
        d = (statistics.fmean(scores["ours"]) - statistics.fmean(scores["original"])) * 100
        print(f"Приріст до наявного порядку: {d:+.1f} в. п.")
    if scores["ours"] and scores["dfe"]:
        d = (statistics.fmean(scores["ours"]) - statistics.fmean(scores["dfe"])) * 100
        print(f"Приріст до DFE:              {d:+.1f} в. п.")

    print()
    print("Опубліковані середні по всьому датасету RTPTorrent (MSR '20, табл. 3):")
    print("   наявний порядок 25,9 %   DFE 81,1 %")
    print("Пряме порівняння коректне лише в межах того самого проєкту.")

    db.close()


if __name__ == "__main__":
    main()
