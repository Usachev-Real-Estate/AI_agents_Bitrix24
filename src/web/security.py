"""Безопасность дашборда: запрет по умолчанию, сессии, CSRF, заголовки.

Требование «чтобы не утекали данные без авторизации» решается не
аккуратностью, а конструкцией. Middleware выполняется ДО роутинга и
пропускает дальше только пути из явного белого списка. Новый эндпоинт
защищён автоматически: разработчику не нужно помнить про декоратор, потому
что декоратора нет. Забыть его невозможно.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.middleware.base import BaseHTTPMiddleware

import store

logger = logging.getLogger(__name__)

COOKIE_SESSION = "dash_session"
COOKIE_CSRF = "dash_csrf"

# Только эти пути доступны без авторизации. Всё остальное — включая
# несуществующие адреса — отдаёт 401/редирект.
PUBLIC_SUFFIXES = frozenset({"/login", "/logout"})
PUBLIC_PREFIX_SUFFIXES = ("/static/",)
HEALTH_PATH = "/healthz"


@dataclass(frozen=True)
class SecurityConfig:
    """Параметры авторизации, снятые с настроек один раз при сборке приложения."""

    base_path: str
    secret_key: str
    cookie_secure: bool = True
    session_ttl_hours: int = 12
    session_idle_hours: int = 2
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    # Машинный доступ к книге клиентов (раздел 7.1 ТЗ). Пустая строка —
    # ветка выключена; проверка это учитывает отдельно, иначе запрос с
    # пустым Bearer прошёл бы на непрописанном токене.
    read_token: str = ""
    write_token: str = ""

    @classmethod
    def from_settings(cls, settings) -> "SecurityConfig":
        secret = (settings.dashboard_secret_key or "").strip()
        if len(secret) < 32:
            # Генерировать ключ на старте нельзя: рестарт разлогинивал бы всех
            # и выглядел бы при этом рабочим. Лучше упасть с внятным текстом.
            raise RuntimeError(
                "DASHBOARD_SECRET_KEY не задан или короче 32 символов. "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
                "и положите в .env"
            )
        read_token = (settings.dossier_read_token or "").strip()
        write_token = (settings.dossier_write_token or "").strip()
        if read_token and read_token == write_token:
            # Один и тот же ключ на чтение и запись отобрать порознь нельзя:
            # сняв право записи, снимаешь и чтение у всех. Падать внятно
            # лучше, чем выбирать за администратора, каким из двух прав
            # считать этот токен, — тем же решением, что и с коротким
            # DASHBOARD_SECRET_KEY выше.
            raise RuntimeError(
                "DOSSIER_READ_TOKEN и DOSSIER_WRITE_TOKEN совпадают. "
                "Право записи тогда не отозвать, не отозвав чтение — "
                "задайте разные значения."
            )
        return cls(
            base_path=(settings.dashboard_base_path or "/dashboard").rstrip("/") or "/dashboard",
            secret_key=secret,
            cookie_secure=bool(settings.dashboard_cookie_secure),
            session_ttl_hours=int(settings.dashboard_session_ttl_hours),
            session_idle_hours=int(settings.dashboard_session_idle_hours),
            login_max_attempts=int(settings.dashboard_login_max_attempts),
            login_lockout_minutes=int(settings.dashboard_login_lockout_minutes),
            read_token=read_token,
            write_token=write_token,
        )

    @property
    def login_url(self) -> str:
        return f"{self.base_path}/login"

    @property
    def signer(self) -> TimestampSigner:
        return TimestampSigner(self.secret_key, salt="dash-session")


def is_public_path(path: str, base_path: str) -> bool:
    """Доступен ли путь без авторизации."""
    if path == HEALTH_PATH:
        return True
    if not path.startswith(base_path):
        return False
    suffix = path[len(base_path):] or "/"
    if suffix in PUBLIC_SUFFIXES:
        return True
    return any(suffix.startswith(prefix) for prefix in PUBLIC_PREFIX_SUFFIXES)


def safe_next(target: str | None, base_path: str) -> str:
    """Отфильтровать адрес возврата после логина.

    Без проверки параметр next превращается в открытый редирект: письмо со
    ссылкой на наш домен уводило бы сотрудника на чужой сайт с формой,
    похожей на нашу.
    """
    if not target:
        return base_path + "/"
    if not target.startswith(base_path):
        return base_path + "/"
    # «//evil.com» и «/\evil.com» браузер трактует как внешний адрес.
    if target.startswith("//") or target.startswith("/\\"):
        return base_path + "/"
    return target


def _is_own_proxy(peer: str) -> bool:
    """Можно ли верить этому соседу на слово про адрес клиента.

    Проверка началась с одного localhost — и была верна, пока дашборд
    запускался прямо на хосте. В контейнере сосед другой: nginx стучится
    через docker-мост, и запрос приходит с адреса шлюза вроде 172.20.0.1.
    Заголовкам не верили, и КАЖДЫЙ клиент получал один и тот же адрес.

    Это ломало не только колонку на экране. Блокировка перебора считает
    неудачи по логину ИЛИ по адресу, и с одним адресом на всех пятеро
    человек, промахнувшихся мимо пароля, запирали дашборд всем сразу —
    включая директора. А защита от подбора по списку логинов, ради которой
    счёт по адресу и заведён, не работала вовсе: различать было нечего.

    Почему частный адрес здесь безопасен. Порт контейнера опубликован
    только на петле хоста (127.0.0.1:8080 в docker-compose), наружу его
    выставляет nginx. Достучаться до дашборда, минуя nginx, неоткуда, и
    значит сосед с частного адреса — это он и есть. С публичного адреса
    заголовки по-прежнему игнорируются: иначе подделать адрес и обойти
    счётчик смог бы кто угодно.
    """
    if peer in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        return ipaddress.ip_address(peer).is_private
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Адрес клиента с учётом обратного прокси."""
    peer = request.client.host if request.client else ""
    if _is_own_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()[:64]
    return peer


# --------------------------------------------------------------------------
# сессии
# --------------------------------------------------------------------------

def issue_session_cookie(response: Response, sid: str, config: SecurityConfig) -> None:
    """Поставить подписанную куку сессии."""
    signed = config.signer.sign(sid.encode()).decode()
    response.set_cookie(
        COOKIE_SESSION,
        signed,
        max_age=config.session_ttl_hours * 3600,
        httponly=True,
        secure=config.cookie_secure,
        samesite="lax",
        path=config.base_path,
    )


def clear_session_cookie(response: Response, config: SecurityConfig) -> None:
    response.delete_cookie(COOKIE_SESSION, path=config.base_path)


def read_sid(request: Request, config: SecurityConfig) -> str | None:
    """Снять подпись с куки. Подделанная кука отсекается без обращения к базе."""
    raw = request.cookies.get(COOKIE_SESSION)
    if not raw:
        return None
    try:
        return config.signer.unsign(
            raw, max_age=config.session_ttl_hours * 3600,
        ).decode()
    except (BadSignature, SignatureExpired):
        return None


# Синтетические «пользователи» машинного доступа. Роль администратора не
# щедрость, а следствие: `Scope.for_user` выдаёт полный доступ только ей, а
# токен по ТЗ и есть доступ ко всему портфелю — наружу читает модель, а не
# человек.
#
# `sid` нет намеренно: сессии у токена не существует, и всё, что её ждёт
# (продление, выход, журнал посещений), обязано об этом узнать по
# отсутствию ключа, а не по совпадению имени.
TOKEN_READER = {"username": "token:read", "role": "admin",
                "department_ids": [], "is_token": True, "can_write": False}
TOKEN_WRITER = {"username": "token:write", "role": "admin",
                "department_ids": [], "is_token": True, "can_write": True}


def bearer_user(request: Request, config: SecurityConfig) -> dict | None:
    """Пользователь по заголовку `Authorization: Bearer`. None — не он.

    Только под `{base_path}/api`. Токен, впускающий на HTML-страницы, — это
    токен, которым однажды откроют дашборд в браузере и оставят вкладку
    незакрытой; а ручки под /api на отказ отвечают 401, а не редиректом на
    форму входа, то есть машина получит код, а не разметку.

    Пустая настройка выключает ветку. Без этой проверки запрос с пустым
    Bearer совпал бы с непрописанным токеном — и книга оказалась бы
    открытой у всех, кто не заполнил `.env`.

    Сравнение постоянного времени и на БАЙТАХ: `hmac.compare_digest` на
    строках с не-ASCII падает TypeError, и подставленный в заголовок
    кириллический мусор давал бы не отказ, а пятисотую. Проект на этом уже
    обжигался в проверке CSRF.
    """
    if not request.url.path.startswith(f"{config.base_path}/api"):
        return None
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    token = value.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    given = token.encode("utf-8", "ignore")
    # Порядок в этой паре не значит ничего: совпасть с обоими токен не
    # может — одинаковые значения `SecurityConfig.from_settings`
    # отвергает на старте. Проверка `known and` тоже страховочная: пустой
    # токен отсеян выше, и с пустой настройкой ему не совпасть. Оба
    # оставлены на случай, если верхнюю проверку однажды ослабят.
    for known, user in ((config.write_token, TOKEN_WRITER),
                        (config.read_token, TOKEN_READER)):
        if known and hmac.compare_digest(given, known.encode("utf-8", "ignore")):
            return dict(user)
    return None


def authenticate(request: Request, config: SecurityConfig) -> dict | None:
    """Вернуть пользователя сессии, машинного пользователя или None.

    Кука проверяется ПЕРВОЙ: человек, открывший дашборд в браузере, должен
    остаться собой со своей областью видимости, даже если в запросе почему-то
    оказался заголовок с токеном.
    """
    sid = read_sid(request, config)
    if sid:
        user = store.touch_session(sid, idle_hours=config.session_idle_hours)
        if user is not None:
            return dict(user, sid=sid)
        return None
    return bearer_user(request, config)


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, config: SecurityConfig) -> None:
    """Положить токен в куку. То же значение уходит скрытым полем формы."""
    response.set_cookie(
        COOKIE_CSRF,
        token,
        max_age=3600,
        httponly=False,
        secure=config.cookie_secure,
        samesite="lax",
        path=config.base_path,
    )


def check_csrf(request: Request, form_token: str | None) -> bool:
    """Двойная отправка: значение из куки должно совпасть со значением формы.

    Сравниваются БАЙТЫ, а не строки. hmac.compare_digest на строках
    отказывается работать, если в них есть что-то кроме ASCII, и падает
    TypeError — то есть подставленный в поле кириллический мусор давал не
    отказ, а пятисотую. Форма входа публична, так что уронить обработчик
    мог кто угодно, не имея учётки. Постоянное время сравнения на байтах
    сохраняется.
    """
    cookie_token = request.cookies.get(COOKIE_CSRF) or ""
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token.encode("utf-8"),
                               form_token.encode("utf-8"))


# --------------------------------------------------------------------------
# middleware
# --------------------------------------------------------------------------

def section_of(path: str, base_path: str) -> str:
    """Раздел, который человек открыл, из адреса запроса.

    Хранится раздел, а не полный адрес: фильтры и строка поиска в след не
    попадают. Вопрос стоит «чем пользуются», а не «что искали», и второй
    ответ дороже первого, не будучи никому нужным.

    Корень отдаётся как redirect на «План на день», и он же потом
    записывается своей строкой — поэтому здесь корень даёт пустоту, а не
    вторую запись о том же открытии.
    """
    tail = path[len(base_path):] if path.startswith(base_path) else path
    tail = tail.strip("/")
    if not tail:
        return ""
    parts = [part for part in tail.split("/") if part][:2]
    return "/".join(parts)[:120]


class RequireAuthMiddleware(BaseHTTPMiddleware):
    """Запрет по умолчанию: всё, что не в белом списке, требует сессии."""

    def __init__(self, app, config: SecurityConfig) -> None:
        super().__init__(app)
        self.config = config

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        if is_public_path(path, self.config.base_path):
            request.state.user = None
            return await call_next(request)

        user = authenticate(request, self.config)
        if user is None:
            return self._deny(request)

        request.state.user = user
        response = await call_next(request)
        # Кнопка «назад» после выхода не должна показывать данные из кеша.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        self._remember(request, user, response)
        return response

    def _remember(self, request: Request, user: dict, response: Response) -> None:
        """Записать, что человек открыл раздел.

        Здесь, а не в обработчиках: через это место проходит КАЖДЫЙ запрос
        к данным, и новый раздел попадёт в след сам. Расставь запись по
        страницам — и первый же добавленный экран окажется невидимым, а
        понять это по журналу нельзя: отсутствие строк выглядит точно так
        же, как «человек туда не заходил».

        Записываются только успешные GET: POST в разделе «Доступы» и так
        пишет свою строку в журнал, отказ разделом не пользование, а
        статика и проверка живости к делу не относятся.
        """
        if request.method != "GET" or response.status_code != 200:
            return
        # След посещений — про людей. Машина читает книгу пачками, и её
        # строки затопили бы журнал, по которому смотрят, кто чем
        # пользуется; учётки `token:read` в базе нет вовсе.
        if user.get("is_token"):
            return
        section = section_of(request.url.path, self.config.base_path)
        if not section:
            return
        store.record_visit(user.get("username", ""), section, client_ip(request))

    def _deny(self, request: Request) -> Response:
        path = request.url.path
        wants_json = (
            path.startswith(f"{self.config.base_path}/api")
            or "application/json" in request.headers.get("accept", "")
        )
        if wants_json:
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        login = f"{self.config.login_url}?next={_quote(target)}"
        return RedirectResponse(login, status_code=303, headers={"Cache-Control": "no-store"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Заголовки безопасности на каждый ответ.

    Строгий CSP без 'unsafe-inline' возможен потому, что весь JS и CSS
    свои: внешних CDN нет. Данные для графиков передаются островками
    <script type="application/json">, которые браузер не исполняет.
    """

    CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )

    def __init__(self, app, *, hsts: bool = True) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()",
        )
        if self.hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains",
            )
        return response


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="/?=&")
