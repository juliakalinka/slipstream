#!/usr/bin/env python3
"""Періодичне перенавчання моделей усіх проєктів.

Навчання вручну придатне для стенда, але не для роботи: історія
накопичується щодня, і моделі, навчені колись, поступово описують уже
інший проєкт. Цей сценарій запускається за розкладом (див.
`deploy/systemd/slipstream-retrain.timer`) і робить дві речі, яких не
робить ендпоінт перенавчання.

**Не навчає без потреби.** Якщо з часу попереднього навчання додалося
менше ніж `--min-new` конвеєрів, проєкт пропускається: нова версія на тих
самих даних лише засмічувала б реєстр версій.

**Не дає тихо погіршити систему.** Реєстр моделей робить кожну нову
версію активною одразу. Для запуску вручну це нормально — людина бачить
метрики, — але вночі нікого немає, і модель, що вийшла гіршою (менша
історія класу, зміна складу перевірок, викид у даних), стала б робочою
сама собою. Тому після навчання якість порівнюється з попередньою
активною версією, і при помітному погіршенні система повертається до неї.
Вимірювання лишається в реєстрі, тому причину видно.

Запуск:
    python scripts/retrain_all.py                 # усі проєкти
    python scripts/retrain_all.py --project 2     # лише один
    python scripts/retrain_all.py --force         # незалежно від приросту
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.ml import registry  # noqa: E402
from app.ml.training import train_all  # noqa: E402
from app.models import ModelType, ModelVersion, Pipeline, Project  # noqa: E402

# Головна метрика кожної моделі й напрям, у якому вона краща.
# Детектор аномалій навчається без розмітки, тому порівнювати його ні з чим:
# «flagged_ratio» описує налаштування, а не якість.
PRIMARY = {
    ModelType.FAILURE: ("roc_auc", "higher"),
    ModelType.DURATION: ("r2", "higher"),
    ModelType.TEST_RANK: ("roc_auc", "higher"),
}

log = logging.getLogger("retrain")


def _new_pipelines_since_last_training(db, project_id: int) -> tuple[int, str]:
    """Скільько конвеєрів завершилося після попереднього навчання."""
    last = db.scalars(
        select(ModelVersion.trained_at)
        .where(ModelVersion.project_id == project_id)
        .order_by(ModelVersion.trained_at.desc())
        .limit(1)
    ).first()

    stmt = select(func.count(Pipeline.id)).where(Pipeline.project_id == project_id)
    if last is not None:
        stmt = stmt.where(Pipeline.started_at > last)
    return db.scalar(stmt) or 0, ("ніколи" if last is None else last.strftime("%Y-%m-%d %H:%M"))


def _active_metrics(db, project_id: int) -> dict[ModelType, tuple[int, float]]:
    """Активна версія кожної моделі та значення її головної метрики."""
    out: dict[ModelType, tuple[int, float]] = {}
    for mtype, (key, _) in PRIMARY.items():
        mv = registry.active(db, project_id, mtype)
        if mv is None:
            continue
        value = (mv.metrics or {}).get(key)
        if isinstance(value, (int, float)):
            out[mtype] = (mv.id, float(value))
    return out


def _guard(db, project_id: int, before: dict[ModelType, tuple[int, float]],
           tolerance: float) -> list[str]:
    """Повернути попередню версію там, де нова вийшла помітно гіршою."""
    reverted: list[str] = []
    for mtype, (key, _) in PRIMARY.items():
        prev = before.get(mtype)
        if prev is None:
            continue                      # порівнювати ні з чим — перше навчання
        prev_id, prev_value = prev

        new_mv = registry.active(db, project_id, mtype)
        if new_mv is None or new_mv.id == prev_id:
            continue                      # нової версії не з'явилося
        new_value = (new_mv.metrics or {}).get(key)
        if not isinstance(new_value, (int, float)):
            continue

        if float(new_value) >= prev_value - tolerance:
            continue

        prev_mv = db.get(ModelVersion, prev_id)
        if prev_mv is None:               # артефакт прибрали — вертатися нікуди
            continue
        new_mv.is_active = False
        prev_mv.is_active = True
        db.commit()
        reverted.append(
            f"{mtype.value}: {key} {prev_value:.4f} -> {float(new_value):.4f}, "
            f"повернено {prev_mv.version}")
    if reverted:
        registry.clear_cache()
    return reverted


def retrain_project(db, project: Project, min_new: int, force: bool,
                    tolerance: float, guard: bool) -> dict:
    new_runs, last_at = _new_pipelines_since_last_training(db, project.id)
    if not force and new_runs < min_new:
        print(f"[-] {project.name}: пропущено, нових конвеєрів {new_runs} "
              f"(потрібно {min_new}), останнє навчання {last_at}")
        return {"skipped": True, "errors": 0}

    print(f"[*] {project.name}: нових конвеєрів {new_runs}, "
          f"останнє навчання {last_at}")
    before = _active_metrics(db, project.id) if guard else {}
    report = train_all(db, project.id, project.default_branch)

    errors = 0
    for name, res in report.models.items():
        if "error" in res:
            print(f"    [!] {name}: {res['error']}")
            errors += 1
        elif res.get("skipped"):
            print(f"    [-] {name}: пропущено ({res.get('reason')})")
        else:
            shown = {k: v for k, v in res["metrics"].items() if k != "feature_importance"}
            keys = ("roc_auc", "r2", "mae_s", "lift_over_random", "flagged_ratio")
            brief = ", ".join(f"{k}={shown[k]}" for k in keys if k in shown)
            print(f"    [+] {name} {res['version']}: {brief}")

    if guard:
        for line in _guard(db, project.id, before, tolerance):
            print(f"    [~] відкат: {line}")

    print(f"    конвеєрів у вибірці: {report.pipelines_used}, "
          f"кандидатів у карантин: {report.flaky_found}")
    return {"skipped": False, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Періодичне перенавчання моделей")
    ap.add_argument("--project", type=int, default=0, help="лише цей проєкт")
    ap.add_argument("--min-new", type=int, default=20,
                    help="мінімальний приріст конвеєрів, щоб навчати заново")
    ap.add_argument("--force", action="store_true", help="навчати незалежно від приросту")
    ap.add_argument("--tolerance", type=float, default=0.03,
                    help="допустиме погіршення головної метрики без відкату")
    ap.add_argument("--no-guard", action="store_true",
                    help="не відкочувати версію, що вийшла гіршою")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    db = SessionLocal()
    try:
        stmt = select(Project).order_by(Project.id)
        if args.project:
            stmt = stmt.where(Project.id == args.project)
        projects = db.scalars(stmt).all()
        if not projects:
            print("Проєктів немає — навчати нічого")
            return 0

        trained = errors = 0
        for p in projects:
            try:
                r = retrain_project(db, p, args.min_new, args.force,
                                    args.tolerance, not args.no_guard)
            except Exception as exc:        # один проєкт не має валити решту
                db.rollback()
                print(f"[!] {p.name}: перенавчання не вдалося: {exc}")
                errors += 1
                continue
            trained += 0 if r["skipped"] else 1
            errors += r["errors"]

        print(f"\nПроєктів оброблено: {len(projects)}, перенавчено: {trained}, "
              f"помилок: {errors}")
        return 1 if errors else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
