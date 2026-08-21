"""Сборка приложения дашборда."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import routes_api
import routes_pages
import store
from config import get_settings
from context import query_string
from format import FILTERS
from schema import init_analytics_db
from security import RequireAuthMiddleware, SecurityConfig, SecurityHeadersMiddleware

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def create_app(settings=None) -> FastAPI:
    """Собрать приложение. Падает сразу, если не настроен секретный ключ."""
    settings = settings or get_settings()
    config = SecurityConfig.from_settings(settings)

    init_analytics_db()
    store.init_store()

    app = FastAPI(
        title="Аналитика Bitrix24",
        # Автодокументация перечислила бы все эндпоинты. В проде она не нужна,
        # а лишний публичный список адресов — лишняя подсказка.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.settings = settings

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(FILTERS)
    templates.env.globals["base_path"] = config.base_path
    templates.env.globals["qs"] = query_string
    app.state.templates = templates

    # Порядок важен: Starlette кладёт добавленный последним снаружи. Заголовки
    # безопасности должны попасть и на ответы-отказы, поэтому они внешние.
    app.add_middleware(RequireAuthMiddleware, config=config)
    app.add_middleware(SecurityHeadersMiddleware, hsts=config.cookie_secure)

    app.mount(
        f"{config.base_path}/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )
    app.include_router(auth.router, prefix=config.base_path)
    app.include_router(routes_api.router, prefix=config.base_path)
    app.include_router(routes_pages.router, prefix=config.base_path)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Проба живости для nginx и мониторинга. Ничего о данных не сообщает."""
        return JSONResponse({"status": "ok"})

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception):
        if request.url.path.startswith(f"{config.base_path}/api"):
            return JSONResponse({"error": "not_found"}, status_code=404)
        return templates.TemplateResponse(
            request, "error.html",
            {"code": 404, "message": "Страница не найдена",
             "base_path": config.base_path, "user": getattr(request.state, "user", None)},
            status_code=404,
        )

    @app.exception_handler(500)
    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        # Трейсбек уходит в лог, но не в ответ: он раскрывает пути, версии
        # и обрывки конфигурации, включая то, чего пользователю видеть нельзя.
        logger.exception("Ошибка обработки %s %s", request.method, request.url.path)
        if request.url.path.startswith(f"{config.base_path}/api"):
            return JSONResponse({"error": "internal_error"}, status_code=500)
        return templates.TemplateResponse(
            request, "error.html",
            {"code": 500, "message": "Внутренняя ошибка. Загляните в логи сервиса.",
             "base_path": config.base_path, "user": getattr(request.state, "user", None)},
            status_code=500,
        )

    logger.info("Дашборд собран: base_path=%s", config.base_path)
    return app
