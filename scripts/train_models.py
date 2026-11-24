"""Навчання всіх моделей проєкту та реєстрація версій."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, init_db  # noqa: E402
from app.ml.training import train_all  # noqa: E402
from app.models import Project  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Навчання моделей оптимізації CI/CD")
    ap.add_argument("--project", type=int, default=1)
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        print(f"Проєкт {args.project} не знайдено. Спершу виконайте scripts/seed_synthetic.py")
        return

    print(f"Навчання моделей для проєкту «{project.name}»...\n")
    report = train_all(db, project.id, project.default_branch)

    for name, res in report.models.items():
        if "error" in res:
            print(f"  [!] {name}: {res['error']}")
            continue
        if res.get("skipped"):
            print(f"  [-] {name}: пропущено ({res.get('reason')})")
            continue
        m = res["metrics"]
        print(f"  [+] {name} {res['version']}")
        for k, v in m.items():
            if k == "feature_importance":
                top = ", ".join(f"{a}={b}" for a, b in list(v.items())[:5])
                print(f"        найвагоміші ознаки: {top}")
            else:
                print(f"        {k}: {v}")
    print(f"\nКонвеєрів у вибірці: {report.pipelines_used}")
    print(f"Кандидатів у карантин (нестабільні тести): {report.flaky_found}")
    db.close()


if __name__ == "__main__":
    main()
