"""Збереження прогнозів моделей та їх зіставлення з фактом.

Прогноз має цінність лише тоді, коли його можна перевірити. Тому кожен
прогноз, на підставі якого сформовано план конвеєра, зберігається разом із
версією моделі, що його видала, а картка прогону показує його поруч із
фактичним результатом. Це і є замкнений контур зворотного зв'язку: спершу
система заявляє, що буде, потім видно, чи вона мала рацію.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ModelType, ModelVersion, Pipeline, Prediction, RunStatus

# ключ у плані -> тип моделі в реєстрі
PLAN_KEYS: dict[str, ModelType] = {
    "failure": ModelType.FAILURE,
    "duration": ModelType.DURATION,
}

_FINISHED = (RunStatus.SUCCESS, RunStatus.FAILED)


def _version_id(db: Session, project_id: int, mtype: ModelType, version: str | None) -> int | None:
    """Знайти запис реєстру за номером версії, зазначеним у плані."""
    if not version:
        return None
    return db.scalar(
        select(ModelVersion.id).where(
            ModelVersion.project_id == project_id,
            ModelVersion.type == mtype,
            ModelVersion.version == version,
        )
    )


def save_plan_snapshot(db: Session, pipeline: Pipeline, plan) -> None:
    """Запам'ятати план, який система справді видала для цього прогону.

    Без знімка план доводиться перераховувати заднім числом, а історія за цей
    час уже змінилася: картка минулого прогону показувала б не те рішення, яке
    ухвалювалося насправді. Для режиму холостого ходу це ще важливіше - саме
    зі знімків видно, скільки часу система зекономила б, якби її ввімкнули.

    Знімок прогону, який СПРАВДІ виконувався за планом, не перезаписується.
    План запитують за парою «коміт - гілка», і запит, зроблений пізніше для
    того самого коміту (наприклад, під час оцінювання «що було б»), інакше
    затирав би запис про те, що насправді виконувалося. Саме так у картці
    з'являлася суперечність: позначка «оптимізований» поряд із планом, у
    якому зазначено, що виконувався повний набір перевірок.
    """
    if pipeline.optimized and pipeline.plan_snapshot:
        return

    data = plan.to_dict()
    pipeline.plan_snapshot = {
        "mode": data["mode"],
        "risk": data["risk"],
        "selected_tests": data["selected_tests"],
        "total_tests": data["total_tests"],
        "shards": data["shards"],
        "est_duration_s": data["est_duration_s"],
        "baseline_duration_s": data["baseline_duration_s"],
        "saving_ratio": data["saving_ratio"],
        "rationale": data["rationale"],
        "fallbacks": data["fallbacks"],
        "model_versions": data["model_versions"],
        "generated_at": data["generated_at"],
    }


def save_plan_predictions(db: Session, pipeline: Pipeline, plan) -> list[Prediction]:
    """Зберегти прогнози, покладені в основу плану.

    Повторний запит плану для того самого прогону не створює дублікатів:
    наявний запис оновлюється. Так у базі лишається саме той прогноз, за
    яким конвеєр виконувався востаннє.
    """
    values = {
        ModelType.FAILURE: (
            float(plan.risk),
            # наскільки оцінка віддалена від порога ухвалення рішення
            round(abs(plan.risk - plan.risk_threshold), 4),
            {
                "threshold": plan.risk_threshold,
                "mode": plan.mode,
                "selected_tests": plan.selected_tests,
                "total_tests": plan.total_tests,
            },
        ),
        ModelType.DURATION: (
            float(plan.est_duration_s),
            0.0,
            {
                "baseline_s": round(plan.baseline_duration_s, 1),
                "shards": plan.shards,
            },
        ),
    }

    saved: list[Prediction] = []
    for key, mtype in PLAN_KEYS.items():
        value, confidence, payload = values[mtype]
        row = db.scalar(
            select(Prediction).where(
                Prediction.pipeline_id == pipeline.id, Prediction.type == mtype
            )
        )
        if row is None:
            row = Prediction(pipeline_id=pipeline.id, type=mtype)
            db.add(row)
        row.value = value
        row.confidence = confidence
        row.payload = payload
        row.model_version_id = _version_id(db, pipeline.project_id, mtype, plan.model_versions.get(key))
        saved.append(row)

    db.commit()
    for row in saved:
        db.refresh(row)
    return saved


def compare_with_fact(db: Session, pipeline: Pipeline) -> list[dict]:
    """Зіставити збережені прогнози з фактичним результатом прогону.

    Доки прогін не завершився, фактичне значення невідоме: повертається
    None, а не нуль, щоб не створювати хибного враження про точність.
    """
    rows = db.scalars(
        select(Prediction)
        .where(Prediction.pipeline_id == pipeline.id)
        .order_by(Prediction.type)
    ).all()

    finished = pipeline.status in _FINISHED
    out: list[dict] = []
    for p in rows:
        if p.type == ModelType.FAILURE:
            actual = (1.0 if pipeline.status == RunStatus.FAILED else 0.0) if finished else None
            unit, label = "", "Failure risk"
        elif p.type == ModelType.DURATION:
            actual = float(pipeline.duration_s) if pipeline.duration_s else None
            unit, label = "s", "Pipeline duration"
        else:
            actual, unit, label = None, "", p.type.value

        error = None
        if actual is not None:
            error = round(abs(p.value - actual), 4)

        out.append({
            "type": p.type.value,
            "label": label,
            "unit": unit,
            "predicted": round(p.value, 4),
            "actual": actual,
            "error": error,
            "confidence": round(p.confidence, 4),
            "model_version": p.model_version.version if p.model_version else None,
            "created_at": p.created_at,
        })
    return out
