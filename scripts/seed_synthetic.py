"""Генератор синтетичної історії CI/CD для розробки та експериментів.

ПРИЗНАЧЕННЯ І ЧЕСНЕ ЗАСТЕРЕЖЕННЯ.
Цей генератор потрібен, щоб система була працездатною з першої хвилини:
на ньому налагоджуються моделі, API та інтерфейс. Для розділу 4 пояснювальної
записки результатів САМЕ НА СИНТЕТИЦІ недостатньо, бо модель відтворює ті
закономірності, які в неї заклали. Основні вимірювання треба робити на
власному стенді GitLab або на відкритих датасетах збірок, а синтетику
використовувати для перевірки крайніх випадків.

Закладені в генератор залежності (те, що моделі мають відновити):
  * ризик збою зростає з обсягом зміни та кількістю зачеплених модулів;
  * автори мають різну якість коду;
  * зміни лише в документації майже ніколи не ламають збірку;
  * кожен тестовий набір «чутливий» до свого підмножини модулів;
  * частина тестів нестабільна і падає випадково, без зв'язку зі змінами;
  * нічні та вихідні коміти дещо ризикованіші.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    CIProvider, Commit, Job, Pipeline, PlanMode, Policy, PolicyType, Project,
    Role, RunStatus, TestCase, TestResult, TestStatus, User,
)
from app.services.security import hash_password  # noqa: E402

# ---------------------------------------------------------------- структура репозиторію
MODULES = {
    "src/auth":    {"weight": 0.18, "risk": 0.30},
    "src/billing": {"weight": 0.16, "risk": 0.38},
    "src/api":     {"weight": 0.22, "risk": 0.26},
    "src/ui":      {"weight": 0.20, "risk": 0.18},
    "src/core":    {"weight": 0.12, "risk": 0.34},
    "infra":       {"weight": 0.06, "risk": 0.22},
    "docs":        {"weight": 0.06, "risk": 0.01},
}

# Кожен набір тестів чутливий до своїх модулів. Саме цю матрицю
# має відновити модель ранжування тестів.
SUITES = {
    "auth":        {"modules": {"src/auth": 0.55, "src/core": 0.18, "src/api": 0.10}, "n": 26, "dur": (400, 2200)},
    "billing":     {"modules": {"src/billing": 0.60, "src/core": 0.15, "src/api": 0.12}, "n": 22, "dur": (600, 3000)},
    "api":         {"modules": {"src/api": 0.50, "src/auth": 0.14, "src/billing": 0.12}, "n": 30, "dur": (300, 1600)},
    "ui":          {"modules": {"src/ui": 0.52, "src/api": 0.14}, "n": 24, "dur": (900, 4500)},
    "integration": {"modules": {"src/api": 0.30, "src/billing": 0.28, "src/auth": 0.25, "src/core": 0.25}, "n": 14, "dur": (3000, 12000)},
    "smoke":       {"modules": {"src/core": 0.20, "src/api": 0.15}, "n": 8, "dur": (200, 700)},
}

AUTHORS = [
    ("olena.kovalenko", 0.55), ("dmytro.shevchuk", 1.60), ("iryna.bondar", 0.85),
    ("andrii.melnyk", 1.25), ("sofiia.tkachenko", 0.50), ("yurii.kravets", 1.05),
]

BRANCHES = ["main"] + [f"feature/task-{i}" for i in range(101, 121)] + [f"fix/bug-{i}" for i in range(50, 60)]


def sha_of(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()


def pick_modules(rng: random.Random) -> list[str]:
    names = list(MODULES)
    weights = [MODULES[m]["weight"] for m in names]
    k = rng.choices([1, 2, 3, 4], weights=[0.55, 0.28, 0.12, 0.05])[0]
    chosen: list[str] = []
    while len(chosen) < k:
        m = rng.choices(names, weights=weights)[0]
        if m not in chosen:
            chosen.append(m)
    return chosen


def make_paths(rng: random.Random, modules: list[str], n_files: int) -> list[str]:
    paths = []
    for _ in range(n_files):
        m = rng.choice(modules)
        if m == "docs":
            paths.append(f"docs/{rng.choice(['guide','api','setup','faq'])}_{rng.randint(1,20)}.md")
        elif m == "infra":
            paths.append(f"infra/{rng.choice(['deploy','terraform','helm'])}/{rng.choice(['main.tf','values.yaml','Dockerfile'])}")
        else:
            paths.append(f"{m}/{rng.choice(['handlers','service','repo','utils','models'])}_{rng.randint(1,15)}.py")
    return sorted(set(paths))


def seed(n_pipelines: int, days: int, seed_value: int, reset: bool) -> None:
    rng = random.Random(seed_value)
    init_db()
    db = SessionLocal()

    if reset:
        for model in (TestResult, Job, Pipeline, Commit, TestCase, Policy, Project, User):
            db.query(model).delete()
        db.commit()
        print("Попередні дані очищено.")

    # --- користувачі
    users = [
        User(email="devops@example.com", full_name="Олена Коваленко",
             password_hash=hash_password("devops123"), role=Role.DEVOPS),
        User(email="dev@example.com", full_name="Дмитро Шевчук",
             password_hash=hash_password("dev123"), role=Role.DEVELOPER),
        User(email="lead@example.com", full_name="Ірина Бондар",
             password_hash=hash_password("lead123"), role=Role.LEAD),
    ]
    db.add_all(users)
    db.commit()

    project = Project(
        name="payments-platform",
        repo_url="https://gitlab.local/team/payments-platform",
        ci_provider=CIProvider.SYNTHETIC,
        external_id="1001",
        default_branch="main",
        plan_mode=PlanMode.DRY_RUN,
        owner_id=users[0].id,
    )
    db.add(project)
    db.commit()

    db.add_all([
        Policy(project_id=project.id, name="Пропускати конвеєр для змін лише в документації",
               type=PolicyType.SKIP_DOCS_ONLY, params={"keep_lint": True}, branch_pattern="*"),
        Policy(project_id=project.id, name="Відбір тестів за бюджетом часу",
               type=PolicyType.TEST_SELECTION,
               params={"budget_ratio": 0.30, "budget_s": 900, "min_keep_ratio": 0.12,
                       "protect_default_branch": True},
               branch_pattern="feature/*,fix/*"),
        Policy(project_id=project.id, name="Розподіл тестів на шарди",
               type=PolicyType.SHARDING, params={"max_shards": 4, "target_s": 80}, branch_pattern="*"),
        Policy(project_id=project.id, name="Впорядкування задач за ризиком",
               type=PolicyType.FAIL_FAST, params={"risk_threshold": 0.45}, branch_pattern="*"),
        Policy(project_id=project.id, name="Карантин нестабільних тестів",
               type=PolicyType.FLAKY_QUARANTINE, params={"threshold": 0.2, "auto": False}, branch_pattern="*"),
    ])
    db.commit()

    # --- тестові набори
    tests: list[TestCase] = []
    flaky_truth: dict[int, float] = {}
    for suite, cfg in SUITES.items():
        for i in range(cfg["n"]):
            tc = TestCase(
                project_id=project.id,
                suite=suite,
                name=f"test_{suite}_{i:02d}",
                file_path=f"tests/{suite}/test_{suite}_{i:02d}.py",
                avg_duration_ms=rng.uniform(*cfg["dur"]),
            )
            tests.append(tc)
    db.add_all(tests)
    db.commit()

    # близько 7% тестів роблю справді нестабільними
    for tc in tests:
        flaky_truth[tc.id] = rng.uniform(0.03, 0.10) if rng.random() < 0.07 else 0.0
    n_flaky = sum(1 for v in flaky_truth.values() if v > 0)

    # Еталонна розмітка: які тести НАСПРАВДІ нестабільні. Використовується
    # лише для оцінки точності детектора і ніколи не подається в моделі.
    import json
    truth_path = Path(__file__).resolve().parents[1] / "data" / "flaky_ground_truth.json"
    truth_path.write_text(json.dumps(
        {str(k): round(v, 4) for k, v in flaky_truth.items() if v > 0},
        ensure_ascii=False, indent=2))

    tests_by_suite: dict[str, list[TestCase]] = {}
    for tc in tests:
        tests_by_suite.setdefault(tc.suite, []).append(tc)

    # частина впалих конвеєрів перезапускається на тому самому коміті -
    # саме ці повтори дають дані для виявлення нестабільних тестів
    RERUN_PROB = 0.12

    # --- генерація конвеєрів
    start = datetime.now(timezone.utc) - timedelta(days=days)
    branch_last_status: dict[str, int] = {}
    n_failed = 0
    step = timedelta(days=days) / max(n_pipelines, 1)

    for idx in range(n_pipelines):
        ts = start + step * idx + timedelta(minutes=rng.uniform(-90, 90))
        # робочі години вірогідніші
        if rng.random() < 0.75:
            ts = ts.replace(hour=rng.randint(9, 19), minute=rng.randint(0, 59))
        else:
            ts = ts.replace(hour=rng.choice([1, 2, 7, 21, 22, 23]), minute=rng.randint(0, 59))

        author, author_risk = rng.choice(AUTHORS)
        branch = rng.choices(BRANCHES, weights=[0.25] + [0.75 / (len(BRANCHES) - 1)] * (len(BRANCHES) - 1))[0]
        modules = pick_modules(rng)
        n_files = min(rng.choices([1, 2, 3, 5, 9, 18], weights=[0.30, 0.25, 0.18, 0.15, 0.08, 0.04])[0], 25)
        paths = make_paths(rng, modules, n_files)
        additions = int(sum(rng.expovariate(1 / 28) for _ in paths))
        deletions = int(additions * rng.uniform(0.1, 0.7))
        docs_only = all(p.startswith("docs/") for p in paths)

        sha = sha_of(project.id, idx, author, ts)
        commit = Commit(
            sha=sha, project_id=project.id, author=author, branch=branch,
            message=f"{rng.choice(['feat','fix','refactor','chore','docs'])}: changes in {modules[0]}",
            files_changed=len(paths), additions=additions, deletions=deletions,
            changed_paths=paths, committed_at=ts,
        )
        db.add(commit)

        # --- ІСТИННА ймовірність падіння (те, що модель має відновити)
        base = 0.012
        churn_factor = min((additions + deletions) / 330.0, 0.32)
        module_risk = max(MODULES[m]["risk"] for m in modules) * 0.42
        breadth = (len(modules) - 1) * 0.050
        off_hours = 0.042 if (ts.hour < 9 or ts.hour > 19 or ts.weekday() >= 5) else 0.0
        prev_fail = 0.07 if branch_last_status.get(branch, 0) else 0.0
        p_fail = base + churn_factor + module_risk + breadth + off_hours + prev_fail
        p_fail *= author_risk
        # Невелика зміна в одному модулі суттєво безпечніша. Цей множник
        # РОЗШИРЮЄ розкид ризику, не піднімаючи його середнього рівня.
        if len(modules) == 1 and (additions + deletions) < 70:
            p_fail *= 0.30
        if docs_only:
            p_fail *= 0.02
        p_fail = min(max(p_fail, 0.005), 0.72)

        will_fail = rng.random() < p_fail

        # Якщо зміна дефектна, обираємо ОДИН винний модуль і невелику кількість
        # тестів, які його покривають. Саме ця залежність «модуль -> тести»
        # є корисним сигналом, який має відновити модель ранжування.
        culprit_tests: set[int] = set()
        if will_fail:
            culprit_module = rng.choice([m for m in modules if m not in ("docs",)] or modules)
            candidates: list[tuple[TestCase, float]] = []
            for suite, cfg in SUITES.items():
                w = cfg["modules"].get(culprit_module, 0.0)
                if w > 0:
                    candidates.extend((tc, w) for tc in tests_by_suite[suite])
            if candidates:
                k = rng.choices([1, 2, 3, 5], weights=[0.45, 0.28, 0.17, 0.10])[0]
                picked = rng.choices(
                    [c[0] for c in candidates],
                    weights=[c[1] for c in candidates],
                    k=min(k, len(candidates)),
                )
                culprit_tests = {tc.id for tc in picked}

        # Впалий конвеєр із певною ймовірністю перезапускають на тому самому
        # коміті. Різниця результатів між спробами і є прямим доказом
        # нестабільності тесту.
        n_attempts = 2 if (will_fail or culprit_tests) and rng.random() < RERUN_PROB else 1
        for attempt in range(n_attempts):
            pipeline = Pipeline(
                project_id=project.id, commit_sha=sha, external_id=f"{90000 + idx}-{attempt}",
                ref=branch, trigger="push", status=RunStatus.RUNNING,
                queued_at=ts + timedelta(minutes=18 * attempt),
                started_at=ts + timedelta(minutes=18 * attempt, seconds=rng.randint(3, 50)),
                optimized=False,
            )
            db.add(pipeline)
            db.flush()

            cursor = pipeline.started_at
            total = 0

            # --- задачі build і lint
            for jname, jstage, lo, hi in [("build", "build", 55, 190), ("lint", "lint", 18, 60)]:
                cache = rng.random() < 0.62
                dur = int(rng.uniform(lo, hi) * (0.55 if cache and jname == "build" else 1.0))
                lint_fail = will_fail and jname == "lint" and rng.random() < 0.18
                job = Job(
                    pipeline_id=pipeline.id, name=jname, stage=jstage,
                    status=RunStatus.FAILED if lint_fail else RunStatus.SUCCESS,
                    runner=f"shared-runner-{rng.randint(1,3)}",
                    started_at=cursor, finished_at=cursor + timedelta(seconds=dur),
                    duration_s=dur, cache_hit=cache,
                )
                db.add(job)
                cursor += timedelta(seconds=dur)
                total += dur

            # --- тестові задачі: повний набір (базова лінія без оптимізації)
            any_test_failed = False
            for suite, cfg in SUITES.items():
                suite_tests = tests_by_suite[suite]
                job = Job(
                    pipeline_id=pipeline.id, name=f"test:{suite}", stage="test",
                    status=RunStatus.SUCCESS, runner=f"shared-runner-{rng.randint(1,3)}",
                    started_at=cursor, duration_s=0, cache_hit=rng.random() < 0.5,
                )
                db.add(job)
                db.flush()

                suite_dur_ms = 0
                suite_failed = False
                for tc in suite_tests:
                    dur_ms = int(max(60, rng.gauss(tc.avg_duration_ms, tc.avg_duration_ms * 0.16)))
                    flaky_p = flaky_truth[tc.id]

                    # істинна причина падіння: або тест покриває дефектну зміну,
                    # або він нестабільний і падає випадково
                    fails_by_change = tc.id in culprit_tests
                    fails_by_flake = rng.random() < flaky_p
                    failed = fails_by_change or fails_by_flake

                    retries = 0
                    if fails_by_flake and not fails_by_change and rng.random() < 0.85:
                        retries = 1  # повторний запуск «вилікував» нестабільний тест
                        failed = False

                    if failed:
                        suite_failed = True
                        any_test_failed = True

                    db.add(TestResult(
                        job_id=job.id, test_case_id=tc.id,
                        status=TestStatus.FAILED if failed else TestStatus.PASSED,
                        duration_ms=dur_ms, retries=retries,
                        error_message="AssertionError" if failed else "",
                    ))
                    tc.total_runs += 1
                    tc.total_failures += int(failed)
                    if failed:
                        tc.last_failed_at = ts
                    suite_dur_ms += dur_ms * (1 + retries)

                sdur = max(1, int(suite_dur_ms / 1000))
                job.duration_s = sdur
                job.finished_at = cursor + timedelta(seconds=sdur)
                job.status = RunStatus.FAILED if suite_failed else RunStatus.SUCCESS
                cursor += timedelta(seconds=sdur)
                total += sdur

            # узгодження підсумкового статусу з реально впалими тестами
            failed_jobs = [j for j in db.query(Job).filter(Job.pipeline_id == pipeline.id).all()
                           if j.status == RunStatus.FAILED]
            final_failed = bool(failed_jobs) or any_test_failed

            if not final_failed and branch == "main":
                dur = rng.randint(40, 120)
                db.add(Job(pipeline_id=pipeline.id, name="deploy", stage="deploy",
                           status=RunStatus.SUCCESS, started_at=cursor,
                           finished_at=cursor + timedelta(seconds=dur), duration_s=dur))
                total += dur

            pipeline.status = RunStatus.FAILED if final_failed else RunStatus.SUCCESS
            pipeline.duration_s = total
            pipeline.queue_time_s = int((pipeline.started_at - ts).total_seconds())
            pipeline.finished_at = pipeline.started_at + timedelta(seconds=total)
            if attempt == n_attempts - 1:
                branch_last_status[branch] = int(final_failed)
                n_failed += int(final_failed)
            if not final_failed:
                break  # повторювати немає сенсу: конвеєр пройшов

        if (idx + 1) % 50 == 0:
            db.commit()
            print(f"  згенеровано {idx + 1}/{n_pipelines} конвеєрів")

    db.commit()

    avg = db.query(Pipeline).filter(Pipeline.project_id == project.id).all()
    mean_dur = sum(p.duration_s for p in avg) / len(avg)
    print()
    print(f"Проєкт:            {project.name} (id={project.id})")
    print(f"Конвеєрів:         {n_pipelines}, з них невдалих {n_failed} ({n_failed/n_pipelines:.1%})")
    print(f"Тестів:            {len(tests)}, справді нестабільних закладено {n_flaky}")
    print(f"Середня тривалість:{mean_dur:.0f} с ({mean_dur/60:.1f} хв)")
    print(f"Період:            {days} днів")
    print()
    print("Облікові записи: devops@example.com / devops123, dev@example.com / dev123")
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Генератор синтетичної історії CI/CD")
    ap.add_argument("--pipelines", type=int, default=400)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="очистити попередні дані")
    args = ap.parse_args()
    seed(args.pipelines, args.days, args.seed, args.reset)
