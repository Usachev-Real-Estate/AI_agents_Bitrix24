"""Вход и выход из дашборда."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import store
from security import (
    check_csrf,
    clear_session_cookie,
    client_ip,
    issue_session_cookie,
    new_csrf_token,
    read_sid,
    safe_next,
    set_csrf_cookie,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Один и тот же текст на «нет такого логина», «неверный пароль» и
# «учётка отключена»: разные тексты превращают форму в перечислитель
# существующих учётных записей.
GENERIC_ERROR = "Неверный логин или пароль"
LOCKED_ERROR = "Слишком много неудачных попыток. Повторите позже."


def _render_login(
    request: Request,
    *,
    error: str = "",
    next_url: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    config = request.app.state.config
    templates = request.app.state.templates
    token = new_csrf_token()
    response: HTMLResponse = templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "next": safe_next(next_url, config.base_path),
            "base_path": config.base_path,
            "csrf_token": token,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, token, config)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "") -> HTMLResponse:
    config = request.app.state.config
    if request.state.user is None and read_sid(request, config):
        pass  # протухшая кука — просто показываем форму
    return _render_login(request, next_url=next)


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
    next: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    config = request.app.state.config
    ip = client_ip(request)

    if not check_csrf(request, csrf_token):
        logger.warning("Вход отклонён: не сошёлся CSRF-токен, ip=%s", ip)
        return _render_login(
            request, error="Сессия формы истекла, попробуйте ещё раз",
            next_url=next, status_code=400,
        )

    if store.is_locked_out(
        username, ip,
        max_attempts=config.login_max_attempts,
        window_minutes=config.login_lockout_minutes,
    ):
        store.record_attempt(username, ip, ok=False)
        logger.warning("Вход заблокирован перебором: логин=%r ip=%s", username[:64], ip)
        return _render_login(request, error=LOCKED_ERROR, next_url=next, status_code=429)

    user = store.verify_password(username, password)
    store.record_attempt(username, ip, ok=user is not None)

    if user is None:
        logger.warning("Неудачный вход: логин=%r ip=%s", username[:64], ip)
        return _render_login(request, error=GENERIC_ERROR, next_url=next, status_code=401)

    sid = store.create_session(
        user["username"],
        ttl_hours=config.session_ttl_hours,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    logger.info("Вход выполнен: %s ip=%s", user["username"], ip)

    response = RedirectResponse(safe_next(next, config.base_path), status_code=303)
    issue_session_cookie(response, sid, config)
    return response


@router.get("/logout")
@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    config = request.app.state.config
    sid = read_sid(request, config)
    if sid:
        store.revoke_session(sid)
    response = RedirectResponse(config.login_url, status_code=303)
    clear_session_cookie(response, config)
    return response
