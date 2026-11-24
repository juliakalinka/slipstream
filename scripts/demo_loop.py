"""Прогін замкненого контуру через справжній HTTP API.

НАВІЩО. Частка оптимізованих прогонів на стенді дорівнює нулю, і це правда:
у режимі холостого ходу план розраховується, але конвеєр виконує повний
набір, тому позначку «оптимізований» ставити нема за що. Питання «а воно
взагалі працює?» має отримати відповідь не поясненням, а прогоном.

Сценарій імітує CI-сервер і проходить обидві фази впровадження:

  1. ХОЛОСТИЙ ХІД. Для наявних прогонів запитується план так само, як це
     робив би плагін у конвеєрі. Система зберігає знімок плану і прогнози.
     Після цього видно, скільки часу вона зекономила б, якби її ввімкнули.

  2. БОЙОВИЙ РЕЖИМ. Кілька нових прогонів проходять повний цикл: запит
     плану, виконання саме цього плану, повернення результату вебхуком із
     позначкою optimized. Це ті самі виклики, які робить справжній конвеєр.

Усе йде через HTTP, а не в обхід через базу: перевіряється саме той шлях,
яким користується CI.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Commit, Pipeline, PlanMode, Project  # noqa: E402


def login(c: httpx.Client, base: str, email: str, password: str) -> dict:
    r = c.post(f"{base}/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def ask_plan(c: httpx.Client, base: str, project_id: int, commit: Commit, ref: str) -> dict:
    """Те саме, що робить cli/cicd_opt.py у задачі «optimize»."""
    r = c.post(f"{base}/pipelines/plan", json={
        "project_id": project_id,
        "commit_sha": commit.sha,
        "ref": ref,
        "author": commit.author,
        "message": commit.message,
        "changed_paths": commit.changed_paths or [],
        "additions": commit.additions,
        "deletions": commit.deletions,
    }, timeout=60.0)
    r.raise_for_status()
    return r.json()


def main() -> None:
    ap = argparse.ArgumentParser(description="Прогін замкненого контуру через API")
    ap.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    ap.add_argument("--project", type=int, default=1)
    ap.add_argument("--email", default="devops@example.com")
    ap.add_argument("--password", default="devops123")
    ap.add_argument("--plans", type=int, default=60,
                    help="для скількох наявних прогонів запитати план (фаза 1)")
    ap.add_argument("--live", type=int, default=12,
                    help="скільки нових прогонів провести в бойовому режимі (фаза 2)")
    args = ap.parse_args()

    base = args.api.rstrip("/")
    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        raise SystemExit("проєкт не знайдено")
    saved_mode = project.plan_mode

    with httpx.Client(timeout=60.0) as c:
        auth = login(c, base, args.email, args.password)

        # ---------------------------------------------------- фаза 1
        rows = list(db.execute(
            select(Pipeline, Commit)
            .join(Commit, Pipeline.commit_sha == Commit.sha)
            .where(Pipeline.project_id == project.id)
            .order_by(Pipeline.started_at.desc())
            .limit(args.plans)
        ).all())
        print("=" * 70)
        print(f"ФАЗА 1. Холостий хід: запит плану для {len(rows)} наявних прогонів")
        print("=" * 70)

        ok = skipped = failed = 0
        savings = []
        for pipeline, commit in rows:
            try:
                plan = ask_plan(c, base, project.id, commit, pipeline.ref)
            except Exception as exc:
                failed += 1
                if failed <= 3:
                    print(f"   ! {commit.sha[:8]}: {exc}")
                continue
            ok += 1
            savings.append(float(plan.get("saving_ratio") or 0.0))
            if plan.get("mode") == "skip":
                skipped += 1

        mean_saving = sum(savings) / len(savings) if savings else 0.0
        print(f"   планів збережено: {ok}, з них пропуск конвеєра: {skipped}, помилок: {failed}")
        print(f"   середня обіцяна економія: {mean_saving:.1%}")

        # ---------------------------------------------------- фаза 2
        if args.live > 0:
            print()
            print("=" * 70)
            print(f"ФАЗА 2. Бойовий режим: {args.live} нових прогонів повним циклом")
            print("=" * 70)

            c.put(f"{base}/projects/{project.id}/mode",
                  headers=auth, json={"plan_mode": "active"}).raise_for_status()
            print("   режим проєкту тимчасово переведено в active")

            base_sources = rows[: args.live]
            now = datetime.now(timezone.utc)
            applied = 0
            saved_seconds = 0.0

            for i, (src_pipe, commit) in enumerate(base_sources):
                ext = f"demo-{int(now.timestamp())}-{i}"
                started = now - timedelta(minutes=(args.live - i) * 7)

                # 1. конвеєр стартував
                c.post(f"{base}/webhooks/ci", json={
                    "project_id": project.id, "event": "pipeline_started",
                    "external_id": ext, "commit_sha": commit.sha, "ref": commit.branch or "main",
                    "status": "running", "author": commit.author, "message": commit.message,
                    "changed_paths": commit.changed_paths or [],
                    "additions": commit.additions, "deletions": commit.deletions,
                }).raise_for_status()

                # 2. службова задача попросила план
                plan = ask_plan(c, base, project.id, commit, commit.branch or "main")
                if not plan.get("applied"):
                    print(f"   {commit.sha[:8]}: план не застосовано ({plan.get('note', '')})")
                    continue

                # 3. конвеєр виконав саме цей план і відзвітував
                est = float(plan.get("est_duration_s") or 0.0)
                baseline = float(plan.get("baseline_duration_s") or est)
                jobs = [
                    {"name": j["name"], "stage": j["stage"], "status": "success",
                     "duration_s": int(j.get("est_duration_s") or 0),
                     "shard_index": (int(j["shard"].split("/")[0]) - 1) if j.get("shard") else None}
                    for j in plan.get("jobs", [])
                ]
                c.post(f"{base}/webhooks/ci", json={
                    "project_id": project.id, "event": "pipeline_finished",
                    "external_id": ext, "commit_sha": commit.sha, "ref": commit.branch or "main",
                    "status": "success", "duration_s": int(est), "queue_time_s": 20,
                    "author": commit.author, "message": commit.message,
                    "changed_paths": commit.changed_paths or [],
                    "jobs": jobs, "optimized": True,
                }).raise_for_status()

                applied += 1
                saved_seconds += max(0.0, baseline - est)
                print(f"   {commit.sha[:8]}  {plan['mode']:<9} "
                      f"{plan['selected_tests']:>3}/{plan['total_tests']:<3} тестів  "
                      f"{est / 60:5.1f} хв замість {baseline / 60:5.1f}  "
                      f"економія {plan['saving_ratio']:.0%}")

            print(f"\n   оптимізованих прогонів: {applied}, "
                  f"зекономлено {saved_seconds / 60:.0f} хв")

        # повертаємо режим, у якому проєкт був до запуску
        c.put(f"{base}/projects/{project.id}/mode",
              headers=auth, json={"plan_mode": saved_mode.value}).raise_for_status()
        print(f"\n   режим повернено: {saved_mode.value}")

    db.close()
    print("\nГотово. Відкрийте Overview: частка оптимізованих прогонів і потенційна")
    print("економія тепер порахуються на справжніх даних.")


if __name__ == "__main__":
    main()
