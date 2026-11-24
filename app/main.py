"""Точка входу застосунку.

Запуск у режимі розробки:
    uvicorn app.main:app --reload
Документація API:
    http://127.0.0.1:8000/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    auth, metrics, models_api, pipelines, plan, problems, projects, recommendations,
    reports, webhooks,
)
from app.config import settings
from app.db import init_db

DESCRIPTION = """
Інформаційна система управління та оптимізації CI/CD конвеєрів у середовищі DevOps.

Система приймає події CI-серверів, накопичує історію збірок і тестів, навчає
моделі машинного навчання та формує оптимізований план виконання конвеєра:
відбір релевантних тестів, розподіл на паралельні шарди, впорядкування задач
за ризиком і пропуск конвеєра для змін, що не впливають на код.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_tags=[
        {"name": "Автентифікація", "description": "Вхід у систему та ролі користувачів"},
        {"name": "Проєкти", "description": "Підключення проєктів і політики оптимізації"},
        {"name": "План конвеєра", "description": "Головний ендпоінт: видача оптимізованого плану"},
        {"name": "Конвеєри", "description": "Історія виконання конвеєрів"},
        {"name": "Події CI", "description": "Приймання вебхуків від CI-серверів"},
        {"name": "Рекомендації", "description": "Пропозиції щодо оптимізації та їх застосування"},
        {"name": "Метрики", "description": "Дашборд, метрики DORA, нестабільні тести, аномалії"},
        {"name": "Моделі", "description": "Версії моделей та перенавчання"},
        {"name": "Звіти", "description": "Показники за довільний період і розсилання зведення"},
        {"name": "Повідомлення про проблеми", "description": "Хибні спрацювання, позначені користувачами"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, projects.router, plan.router, pipelines.router,
          webhooks.router, recommendations.router, metrics.router, models_api.router,
          reports.router, problems.router):
    app.include_router(r, prefix=settings.api_prefix)


@app.get("/health", tags=["Службові"], summary="Перевірка працездатності")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": "1.0.0"}


# ------------------------------------------------------- веб-інтерфейс
# Зібраний інтерфейс віддається тим самим застосунком, тому для запуску
# стенда достатньо однієї команди. Якщо збірки немає, коренева адреса
# повертає службову відповідь, а API працює як звичайно.
WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"

if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """Віддача односторінкового застосунку.

        Спершу пробуємо реальний файл зі збірки (іконка, маніфест тощо).
        Якщо такого файлу немає - повертаємо index.html, щоб працювали
        прямі посилання й оновлення сторінки. Службові префікси сюди не
        потрапляють, бо їхні маршрути оголошені раніше.
        """
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json", "health")):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ресурс не знайдено")

        candidate = (WEB_DIST / full_path).resolve()
        # захист від виходу за межі каталогу збірки
        if candidate.is_file() and WEB_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")

else:
    @app.get("/", tags=["Службові"], summary="Кореневий ресурс")
    def root() -> dict:
        return {
            "name": settings.app_name,
            "docs": "/docs",
            "api": settings.api_prefix,
            "web": "інтерфейс не зібрано: виконайте npm run build у каталозі web",
        }
