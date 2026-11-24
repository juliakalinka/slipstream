"""APFD базових методів упорядкування з самого датасету RTPTorrent.

Датасет містить не лише сирі результати, а й каталог baseline/ з готовими
перевпорядкуваннями тих самих складань сімома методами:

    untreated                  порядок, у якому тести справді виконувалися
    random                     випадковий порядок
    recently-failed            пріоритет тестам, які падали нещодавно
    matrix-naive               матричний метод, проста оцінка
    matrix-conditional-prob    матричний метод, умовна ймовірність
    optimal-failure            верхня межа: спершу всі тести, що впали
    optimal-failure-duration   верхня межа з урахуванням тривалості

Два останні - це не метод, а стеля: вони знають майбутнє. Їхнє значення
показує, наскільки взагалі можна наблизитися до ідеалу на цих даних.

Формат файлів baseline збігається з основним, але колонка index містить
позицію в ПЕРЕВПОРЯДКОВАНІЙ послідовності. Тому APFD рахується однаково для
всіх методів, і порівняння із запропонованим методом є коректним.

Щоб порівняння було на тих самих складаннях, що й у нашому експерименті,
передайте --project-id: тоді враховуються лише ті складання, які потрапили
до бази системи.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ml.test_selector import apfd  # noqa: E402

ORDER = [
    "untreated", "random", "recently-failed",
    "matrix-naive", "matrix-conditional-prob",
    "optimal-failure", "optimal-failure-duration",
]
LABELS = {
    "untreated": "Наявний порядок (без упорядкування)",
    "random": "Випадковий порядок",
    "recently-failed": "Пріоритет тим, що падали нещодавно",
    "matrix-naive": "Матричний метод, проста оцінка",
    "matrix-conditional-prob": "Матричний метод, умовна ймовірність",
    "optimal-failure": "Стеля: спершу всі, що впали",
    "optimal-failure-duration": "Стеля з урахуванням тривалості",
}


def apfd_of_file(path: Path, only_jobs: set[str] | None) -> list[float]:
    """APFD кожного складання у файлі базового методу."""
    per_job: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
    with path.open(newline="", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (rdr.fieldnames or [])}
        job_c = cols.get("travisjobid") or cols.get("tr_job_id")
        test_c = cols.get("testname") or cols.get("test")
        idx_c = cols.get("index")
        fail_c, err_c = cols.get("failures"), cols.get("errors")
        if not (job_c and test_c and idx_c):
            raise SystemExit(f"{path.name}: несподівані колонки {rdr.fieldnames}")

        for row in rdr:
            job = (row.get(job_c) or "").strip()
            if only_jobs is not None and job not in only_jobs:
                continue

            def num(c):
                v = (row.get(c) or "").strip() if c else ""
                try:
                    return float(v) if v else 0.0
                except ValueError:
                    return 0.0

            try:
                idx = int(float(row[idx_c]))
            except (ValueError, TypeError):
                continue
            failed = num(fail_c) > 0 or num(err_c) > 0
            per_job[job].append((idx, (row.get(test_c) or "").strip(), failed))

    out: list[float] = []
    for rows in per_job.values():
        rows.sort(key=lambda r: r[0])
        order = [name for _, name, _ in rows]
        failing = {name for _, name, f in rows if f}
        if failing and len(order) > 1:
            out.append(apfd(order, failing))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="APFD базових методів RTPTorrent")
    ap.add_argument("--root", required=True, help="каталог розпакованого датасету")
    ap.add_argument("--project", required=True, help="назва каталогу проєкту, напр. square@okhttp")
    ap.add_argument("--project-id", type=int, default=0,
                    help="ід проєкту в базі: обмежити тими самими складаннями")
    ap.add_argument("--only-jobs", default="",
                    help="файл з переліком складань від compare_baselines: "
                         "гарантує, що всі методи виміряно на однаковому наборі")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    dirs = [d for d in root.rglob(args.project) if (d / "baseline").is_dir()]
    if not dirs:
        raise SystemExit(f"не знайдено каталог baseline для {args.project}")
    bdir = dirs[0] / "baseline"

    only_jobs: set[str] | None = None
    if args.only_jobs:
        only_jobs = {
            ln.strip() for ln in Path(args.only_jobs).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }
        print(f"Обмеження: {len(only_jobs)} складань із переліку {args.only_jobs}")
    elif args.project_id:
        from sqlalchemy import select

        from app.db import SessionLocal, init_db
        from app.models import Pipeline
        init_db()
        db = SessionLocal()
        only_jobs = {
            str(e) for e in db.scalars(
                select(Pipeline.external_id).where(Pipeline.project_id == args.project_id)
            )
        }
        db.close()
        print(f"Обмеження: {len(only_jobs)} складань, наявних у базі системи")

    print("=" * 74)
    print(f"БАЗОВІ МЕТОДИ RTPTorrent — {args.project}")
    print("=" * 74)
    print(f"{'Метод':<40}{'APFD, середнє':>16}{'складань':>12}")
    print("-" * 74)

    for key in ORDER:
        matches = list(bdir.glob(f"*@{key}.csv"))
        if not matches:
            continue
        vals = apfd_of_file(matches[0], only_jobs)
        if not vals:
            print(f"{LABELS[key]:<40}{'немає даних':>16}{0:>12}")
            continue
        print(f"{LABELS[key]:<40}{statistics.fmean(vals) * 100:>15.1f}%{len(vals):>12}")
    print("-" * 74)
    print("Стеля знає майбутнє і методом не є: вона показує межу можливого.")


if __name__ == "__main__":
    main()
