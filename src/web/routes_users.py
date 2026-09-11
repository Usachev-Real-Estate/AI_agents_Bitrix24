"""Раздел «Доступы»: завести человека, сменить пароль, закрыть доступ.

Всё то же самое умеет `manage.py`, и раздел его не заменяет — он снимает
условие «нужен ssh и человек, который помнит команды». Онбординг брокера и
особенно закрытие доступа уволенному нужны в ту минуту, когда о них
вспомнили, а не когда до сервера дойдут руки.

Три решения, объясняющих устройство.

**Пароль придумывает браузер, а не сервер.** Кнопка «Сгенерировать»
заполняет поле на стороне админа, он копирует значение и только потом
отправляет форму. Так ответ сервера не содержит пароля вовсе: его нечего
показывать на экране, нечего потерять при обновлении страницы и нечего
случайно оставить в истории. Сгенерируй сервер — пришлось бы показать
результат, а обновление страницы выдало бы уже другой пароль, и розданный
перестал бы работать. Скрипта нет — поле обычное, пароль вводится руками.

**Каждое действие — POST с редиректом.** Обновление страницы после
«сменить пароль» не должно менять пароль второй раз.

**Себя изменить нельзя.** Ни снять с себя администратора, ни отключить
себя. Это не забота о чувствах: администратор — единственная роль, которая
может раздавать роли, и учётка, разжаловавшая сама себя, закрывает раздел
для всех. Запрет на себя гарантирует, что администратор останется хотя бы
один, без пересчёта их числа.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import metrics
import store
from context import base_context, read_analytics
from security import check_csrf, new_csrf_token, set_csrf_cookie

logger = logging.getLogger(__name__)

router = APIRouter()

SLUG = "users"

# Что показать после действия. Текст короткий и говорит, что именно
# произошло: «Сохранено» не отличает смену роли от смены пароля, а это
# разные по последствиям вещи.
DONE = {
    "created": "Пользователь заведён. Передайте ему логин и пароль.",
    "password": "Пароль изменён, прежние сессии закрыты.",
    "role": "Права изменены — они действуют уже сейчас, перезаходить не нужно.",
    "enabled": "Доступ открыт.",
    "disabled": "Доступ закрыт, сессии завершены.",
}

FAILED = {
    "csrf": "Форма устарела — страница перезагружена, повторите действие.",
    "login": "Логин пустой или занят.",
    "password": f"Пароль короче {store.MIN_PASSWORD_LEN} символов.",
    "missing": "Такого пользователя нет.",
    "self": "Свою собственную учётку менять здесь нельзя.",
    "role": "Неизвестная роль.",
}


# Окна следа. Неделя — «пользуется ли сейчас», месяц и квартал — «когда
# заходил в последний раз»; у бросившего заходить ответы расходятся.
VISIT_WINDOWS: tuple[tuple[int, str], ...] = (
    (7, "неделя"), (30, "месяц"), (90, "квартал"),
)

# Человеческие названия разделов. В следе лежит адрес — он короткий и
# устойчивый, — а на экране нужно слово, которое стоит в меню.
SECTION_LABEL: dict[str, str] = {
    "today": "План на день",
    "pulse": "Пульс",
    "leads": "Лиды",
    "deals": "Сделки",
    "movement": "Движение",
    "people": "Люди",
    "objects": "Объекты",
    "table": "Таблица",
    "quality": "Качество данных",
    "users": "Доступы",
    "api/export.csv": "Выгрузка CSV",
}


def _visit_days(requested: str | None) -> int:
    """Окно следа из адреса. Мусор молча падает в неделю."""
    allowed = {days for days, _ in VISIT_WINDOWS}
    try:
        value = int(requested or 0)
    except ValueError:
        return VISIT_WINDOWS[0][0]
    return value if value in allowed else VISIT_WINDOWS[0][0]


def _back(request: Request, *, done: str = "", failed: str = "") -> RedirectResponse:
    """Вернуться на страницу, сказав, чем кончилось.

    Редирект, а не отрисовка ответа на POST: иначе обновление страницы
    повторило бы действие, и «сменить пароль» сменило бы его дважды —
    второй раз уже на значение, которого администратор не видел.
    """
    base = request.app.state.config.base_path
    query = f"?done={done}" if done else (f"?failed={failed}" if failed else "")
    return RedirectResponse(f"{base}/{SLUG}{query}", status_code=303)


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, "user", None) or {}
    return user.get("role") == store.ROLE_ADMIN


def _me(request: Request) -> str:
    return ((getattr(request.state, "user", None) or {}).get("username") or "").lower()


def _deny(request: Request) -> HTMLResponse:
    """Отказ без подробностей: чего нет, того не видно."""
    config = request.app.state.config
    return request.app.state.templates.TemplateResponse(
        request, "error.html",
        {"code": 403, "message": "Раздел доступен только администратору",
         "base_path": config.base_path,
         "user": getattr(request.state, "user", None)},
        status_code=403,
    )


def _departments(request: Request) -> list[dict]:
    with read_analytics(request) as conn:
        return metrics.departments_options(conn)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request) -> HTMLResponse:
    if not _is_admin(request):
        return _deny(request)
    context = base_context(request, active=SLUG)
    token = new_csrf_token()
    params = request.query_params
    # Окно следа выбирается чипом. Неделя отвечает на «пользуется ли
    # сейчас», месяц — на «пользовался ли вообще»: вопросы разные, и
    # ответы на них расходятся ровно у того, кто бросил заходить.
    days = _visit_days(params.get("days"))
    context.update({
        "users": store.list_users(),
        "visits": store.visit_summary(days),
        "visit_days": days,
        "visit_windows": VISIT_WINDOWS,
        "recent": store.recent_visits(40),
        "sections": SECTION_LABEL,
        "sessions": store.list_sessions(None),
        "departments": _departments(request),
        "roles": store.ROLES,
        "role_admin": store.ROLE_ADMIN,
        "min_password": store.MIN_PASSWORD_LEN,
        "me": _me(request),
        "done": DONE.get(params.get("done", "")),
        "failed": FAILED.get(params.get("failed", "")),
        "csrf_token": token,
    })
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "users.html", context,
    )
    set_csrf_cookie(response, token, request.app.state.config)
    return response


def _guard(request: Request, csrf_token: str) -> Response | None:
    """Общая проверка на каждое действие: администратор и живая форма.

    Не-администратору — тот же отказ, что и на самой странице, а не
    редирект с сообщением: сообщение пришлось бы придумывать, и любое из
    них рассказывало бы о разделе больше, чем следует.
    """
    if not _is_admin(request):
        return _deny(request)
    if not check_csrf(request, csrf_token):
        logger.warning("Действие с доступами отклонено: не сошёлся CSRF-токен")
        return _back(request, failed="csrf")
    return None


@router.post("/users/create")
async def create(
    request: Request,
    username: str = Form(default=""),
    display_name: str = Form(default=""),
    password: str = Form(default=""),
    role: str = Form(default=store.ROLE_ROP),
    department: list[int] = Form(default=[]),
    csrf_token: str = Form(default=""),
) -> Response:
    stop = _guard(request, csrf_token)
    if stop is not None:
        return stop

    login = username.strip().lower()
    if not login or login in {row["username"] for row in store.list_users()}:
        return _back(request, failed="login")
    if role not in store.ROLES:
        return _back(request, failed="role")
    # Отделы у администратора не хранятся — он видит всё. Присланные из
    # формы молча отбрасываются, чтобы галочка, забытая при смене роли, не
    # оседала в базе и не сбивала с толку при следующем чтении.
    departments = [] if role == store.ROLE_ADMIN else department
    try:
        store.create_user(login, password, display_name.strip(),
                          role=role, department_ids=departments)
    except ValueError:
        return _back(request, failed="password")
    logger.info("Заведена учётка %s, роль %s, отделы %s", login, role, departments)
    return _back(request, done="created")


@router.post("/users/password")
async def password(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    stop = _guard(request, csrf_token)
    if stop is not None:
        return stop

    try:
        # set_password меняет ТОЛЬКО пароль. create_user на существующем
        # логине переписал бы строку целиком и разжаловал администратора в
        # РОПа без отделов — молча, потому что вход продолжал бы работать.
        changed = store.set_password(username, password)
    except ValueError:
        return _back(request, failed="password")
    if not changed:
        return _back(request, failed="missing")
    # Сессии закрываются всегда: иначе тот, ради кого меняли пароль,
    # продолжит сидеть в дашборде по своей куке.
    revoked = store.revoke_all_sessions(username)
    logger.info("Пароль изменён: %s, отозвано сессий %d", username, revoked)
    return _back(request, done="password")


@router.post("/users/role")
async def role(
    request: Request,
    username: str = Form(default=""),
    role: str = Form(default=store.ROLE_ROP),
    department: list[int] = Form(default=[]),
    csrf_token: str = Form(default=""),
) -> Response:
    stop = _guard(request, csrf_token)
    if stop is not None:
        return stop

    login = username.strip().lower()
    if login == _me(request):
        return _back(request, failed="self")
    if role not in store.ROLES:
        return _back(request, failed="role")
    departments = [] if role == store.ROLE_ADMIN else department
    if not store.set_user_role(login, role, departments):
        return _back(request, failed="missing")
    logger.info("Права изменены: %s → %s, отделы %s", login, role, departments)
    return _back(request, done="role")


@router.post("/users/active")
async def active(
    request: Request,
    username: str = Form(default=""),
    enabled: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Response:
    stop = _guard(request, csrf_token)
    if stop is not None:
        return stop

    login = username.strip().lower()
    if login == _me(request):
        return _back(request, failed="self")
    turn_on = enabled == "1"
    if not store.set_user_active(login, turn_on):
        return _back(request, failed="missing")
    if turn_on:
        logger.info("Доступ открыт: %s", login)
        return _back(request, done="enabled")
    # Закрытие доступа обязано быть мгновенным — ради этого и держатся
    # серверные сессии, а не только подписанная кука.
    revoked = store.revoke_all_sessions(login)
    logger.info("Доступ закрыт: %s, отозвано сессий %d", login, revoked)
    return _back(request, done="disabled")
