"""Імпорт відкритого каталогу нестабільних тестів IDoFT.

IDoFT (International Dataset of Flaky Tests) - це курований дослідницький
набір Університету Іллінойсу: понад вісім тисяч РОЗМІЧЕНИХ ВРУЧНУ нестабільних
тестів із сотень відкритих проєктів, із зазначенням причини нестабільності.

ЩО ЦЕЙ НАБІР ДАЄ І ЧОГО НЕ ДАЄ.
Він НЕ містить історії запусків, тому навчити на ньому детектор нестабільності
неможливо: детектору потрібні повторні прогони того самого тесту на тому самому
коді. Натомість набір дає дві речі, важливі для роботи:

  1. Реальну таксономію причин нестабільності з реальними частками - матеріал
     для підрозділу 1.3 пояснювальної записки.
  2. Перелік тестів, ЗАЛЕЖНИХ ВІД ПОРЯДКУ виконання (категорії OD, OD-Vic,
     OD-Brit, NDOD). Це критично: система переставляє тести й розкидає їх по
     шардах, а отже для таких тестів може САМА створити збій. Позначка
     order_dependent вимикає для них переставляння.

Категорії IDoFT:
    OD      - залежить від порядку виконання (order-dependent)
    OD-Vic  - "жертва": падає, якщо перед ним не відпрацював інший тест
    OD-Brit - "винуватець": ламає інші тести своїм побічним ефектом
    NOD     - недетермінований, падає випадково
    NDOD    - недетермінований і залежний від порядку водночас
    ID      - залежить від реалізації (implementation-dependent)
    NIO     - непослідовний у ізоляції
    UD      - невизначений
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Project, TestCase  # noqa: E402

IDOFT_URL = "https://raw.githubusercontent.com/TestingResearchIllinois/idoft/main/pr-data.csv"
LOCAL_COPY = Path(__file__).resolve().parents[1] / "data" / "external" / "idoft_pr_data.csv"

# Категорії, за яких переставляти тест НЕБЕЗПЕЧНО
ORDER_DEPENDENT = {"OD", "OD-Vic", "OD-Brit", "NDOD", "NDOD;TD"}

CATEGORY_HELP = {
    "OD": "залежить від порядку виконання",
    "OD-Vic": "жертва порядку: потребує попереднього тесту",
    "OD-Brit": "винуватець: ламає наступні тести",
    "NOD": "недетермінований",
    "NDOD": "недетермінований і залежний від порядку",
    "ID": "залежить від реалізації",
    "NIO": "непослідовний у ізоляції",
    "UD": "невизначена причина",
    "TD": "залежить від часу",
}


def fetch(use_cache: bool = True) -> str:
    if use_cache and LOCAL_COPY.exists():
        print(f"Використано локальну копію {LOCAL_COPY.name}")
        return LOCAL_COPY.read_text(encoding="utf-8", errors="ignore")
    print(f"Завантаження {IDOFT_URL}")
    req = urllib.request.Request(IDOFT_URL, headers={"User-Agent": "cicd-optimizer-research"})
    text = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "ignore")
    LOCAL_COPY.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_COPY.write_text(text, encoding="utf-8")
    return text


def summarize(rows: list[dict]) -> dict:
    """Статистика каталогу: готовий матеріал для розділу 1 записки."""
    cats = Counter((r.get("Category") or "").strip() for r in rows)
    projects = {(r.get("Project URL") or "").strip() for r in rows}
    od = sum(n for c, n in cats.items() if c in ORDER_DEPENDENT)

    print("\n" + "=" * 68)
    print("КАТАЛОГ НЕСТАБІЛЬНИХ ТЕСТІВ IDoFT")
    print("=" * 68)
    print(f"Записів:            {len(rows)}")
    print(f"Проєктів:           {len(projects)}")
    print(f"Залежних від порядку виконання: {od} ({od / len(rows):.1%})")
    print("\nРозподіл за причиною нестабільності:")
    for c, n in cats.most_common(9):
        mark = " <- переставляти небезпечно" if c in ORDER_DEPENDENT else ""
        print(f"   {c or '(не вказано)':10} {n:5} {n / len(rows):6.1%}  "
              f"{CATEGORY_HELP.get(c, '')}{mark}")
    return {"total": len(rows), "projects": len(projects), "order_dependent": od,
            "categories": dict(cats)}


def apply_to_project(db, project: Project, rows: list[dict]) -> dict:
    """Позначити тести проєкту, які збігаються з каталогом.

    Зіставлення виконується за назвою тесту: у каталозі вона повністю
    кваліфікована (пакет.Клас.метод), у нас зберігається коротка назва.
    Порівнюємо останній сегмент.
    """
    by_name: dict[str, str] = {}
    for r in rows:
        fq = (r.get("Fully-Qualified Test Name (packageName.ClassName.methodName)") or "").strip()
        cat = (r.get("Category") or "").strip()
        if not fq:
            continue
        short = fq.split(".")[-1]
        # якщо тест уже є, лишаємо найнебезпечнішу категорію
        if short not in by_name or (cat in ORDER_DEPENDENT and by_name[short] not in ORDER_DEPENDENT):
            by_name[short] = cat

    tests = db.scalars(select(TestCase).where(TestCase.project_id == project.id)).all()
    matched = od_marked = 0
    for t in tests:
        cat = by_name.get(t.name)
        if cat is None:
            continue
        matched += 1
        t.flaky_category = cat[:24]
        if cat in ORDER_DEPENDENT:
            t.order_dependent = True
            od_marked += 1
    db.commit()
    return {"tests": len(tests), "matched": matched, "order_dependent": od_marked}


def main() -> None:
    ap = argparse.ArgumentParser(description="Імпорт каталогу нестабільних тестів IDoFT")
    ap.add_argument("--project", type=int, default=0,
                    help="позначити тести цього проєкту за каталогом")
    ap.add_argument("--refresh", action="store_true", help="перезавантажити з мережі")
    ap.add_argument("--mark-share", type=float, default=0.0,
                    help="демонстраційно позначити задану частку тестів як залежні від "
                         "порядку, якщо збігів за назвою немає (лише для стенда)")
    args = ap.parse_args()

    init_db()
    rows = list(csv.DictReader(io.StringIO(fetch(use_cache=not args.refresh))))
    stats = summarize(rows)

    if not args.project:
        print("\nЩоб позначити тести проєкту, вкажіть --project <id>")
        return

    db = SessionLocal()
    project = db.get(Project, args.project)
    if project is None:
        print(f"Проєкт {args.project} не знайдено")
        return

    res = apply_to_project(db, project, rows)
    print(f"\nПроєкт «{project.name}»: тестів {res['tests']}, "
          f"збіглося з каталогом {res['matched']}, з них залежних від порядку {res['order_dependent']}")

    if res["matched"] == 0 and args.mark_share > 0:
        # Стенд згенерований синтетично, тому назв із каталогу в ньому немає.
        # Щоб перевірити РОБОТУ захисту від переставляння, позначаємо частку
        # тестів вручну. Це навчальний прийом, а не дані з каталогу.
        import random
        rng = random.Random(42)
        tests = db.scalars(select(TestCase).where(TestCase.project_id == project.id)).all()
        share = min(max(args.mark_share, 0.0), 0.5)
        picked = rng.sample(tests, int(len(tests) * share))
        for t in picked:
            t.order_dependent = True
            t.flaky_category = "OD"
        db.commit()
        print(f"Демонстраційно позначено {len(picked)} тестів як залежні від порядку "
              f"(частка {share:.0%}, взята з реального розподілу IDoFT)")

    print("\nЗастереження: IDoFT не містить історії запусків, тому навчити на ньому")
    print("детектор нестабільності неможливо. Він дає таксономію причин і перелік")
    print("тестів, які не можна переставляти.")
    db.close()


if __name__ == "__main__":
    main()
