"""Авторизация дашборда: главное требование — данные не утекают без входа.

Проверка построена на переборе маршрутов приложения, а не на списке
известных адресов: новый эндпоинт, забытый разработчиком, обязан провалить
этот тест, иначе смысла в нём нет.
"""

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from config import get_settings
from links import crm_link, portal_domain
from security import SecurityConfig, is_public_path, safe_next

BASE = "/dashboard"
PUBLIC = {f"{BASE}/login", f"{BASE}/logout", "/healthz"}
LOGIN = "tester"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def dash_app(analytics_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "x" * 48)
    # TestClient ходит по http; кука с флагом Secure до сервера бы не доехала,
    # и все проверки входа падали бы по постороннему поводу. Сам флаг проверен
    # отдельным тестом.
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    application = create_app()
    store.create_user(LOGIN, PASSWORD, "Тестовый Пользователь", role="admin")
    return application


@pytest.fixture
def client(dash_app):
    return TestClient(dash_app, follow_redirects=False)


@pytest.fixture
def auth_client(dash_app):
    session = TestClient(dash_app, follow_redirects=False)
    page = session.get(f"{BASE}/login")
    token = session.cookies.get("dash_csrf")
    response = session.post(
        f"{BASE}/login",
        data={"username": LOGIN, "password": PASSWORD, "csrf_token": token, "next": ""},
    )
    assert response.status_code == 303, page.text[:400]
    return session


# --------------------------------------------------------------------------
# запрет по умолчанию
# --------------------------------------------------------------------------

def _app_paths(app) -> list[str]:
    """Все зарегистрированные адреса, включая вложенные роутеры.

    FastAPI не разворачивает включённые роутеры в app.routes, поэтому обходим
    и их: иначе перебор молча проверял бы один /healthz и всегда был бы
    зелёным.
    """
    paths: list[str] = []
    for route in app.routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            for sub in included.routes:
                sub_path = getattr(sub, "path", "")
                if sub_path:
                    paths.append(f"{BASE}{sub_path}")
            continue
        path = getattr(route, "path", "")
        if path and "{" not in path and not path.startswith(f"{BASE}/static"):
            paths.append(path)
    return sorted(set(paths))


def test_route_enumeration_actually_finds_the_routes(dash_app):
    """Страховка на саму проверку: список адресов не должен схлопнуться в пустой."""
    paths = _app_paths(dash_app)
    assert f"{BASE}/" in paths
    assert f"{BASE}/api/overview" in paths
    assert f"{BASE}/table" in paths
    assert len(paths) >= 14


def test_every_route_denies_anonymous_access(client, dash_app):
    """Перебор маршрутов: забытый эндпоинт обязан провалить этот тест."""
    checked = 0
    for path in _app_paths(dash_app):
        if path in PUBLIC:
            continue
        response = client.get(path)
        assert response.status_code in (401, 303), f"{path} отдал {response.status_code}"
        if response.status_code == 303:
            assert response.headers["location"].startswith(f"{BASE}/login")
        checked += 1
    assert checked >= 10, "маршруты не найдены — тест перестал что-либо проверять"


def test_anonymous_responses_carry_no_data(client, dash_app):
    """Отказ обязан быть пустым: подсказка о содержимом — тоже утечка."""
    for path in _app_paths(dash_app):
        if path in PUBLIC:
            continue
        response = client.get(path, headers={"accept": "application/json"})
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}


def test_unknown_paths_are_denied_not_disclosed(client):
    """Несуществующий адрес тоже требует входа — список эндпоинтов не разглашается."""
    for path in (f"{BASE}/secret", f"{BASE}/api/secret", f"{BASE}/api/../etc"):
        response = client.get(path, headers={"accept": "application/json"})
        assert response.status_code == 401


def test_api_export_requires_auth(client):
    response = client.get(f"{BASE}/api/export.csv")
    assert response.status_code in (401, 303)
    assert b"deal" not in response.content


def test_healthz_is_public_and_says_nothing(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_docs_are_disabled(client):
    """Автодокументация перечислила бы все эндпоинты одним списком."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code in (401, 404, 303)


@pytest.mark.parametrize("path,expected", [
    (f"{BASE}/login", True),
    (f"{BASE}/logout", True),
    (f"{BASE}/static/css/app.css", True),
    ("/healthz", True),
    (f"{BASE}/", False),
    (f"{BASE}/api/overview", False),
    (f"{BASE}/loginx", False),
    ("/dashboardx/login", False),
    (f"{BASE}/static", False),
])
def test_public_path_whitelist(path, expected):
    assert is_public_path(path, BASE) is expected


# --------------------------------------------------------------------------
# вход
# --------------------------------------------------------------------------

def test_login_succeeds_and_grants_access(auth_client):
    response = auth_client.get(f"{BASE}/today", follow_redirects=False)
    assert response.status_code == 200
    assert "План на день" in response.text


def test_logout_revokes_the_session(auth_client):
    auth_client.get(f"{BASE}/logout")
    response = auth_client.get(f"{BASE}/")
    assert response.status_code == 303


def test_wrong_password_is_rejected(client):
    client.get(f"{BASE}/login")
    response = client.post(f"{BASE}/login", data={
        "username": LOGIN, "password": "wrong-password-entirely",
        "csrf_token": client.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 401


def test_unknown_user_and_wrong_password_are_indistinguishable(client):
    """Разные тексты ошибок превратили бы форму в перечислитель учёток."""
    def attempt(username, password):
        client.get(f"{BASE}/login")
        return client.post(f"{BASE}/login", data={
            "username": username, "password": password,
            "csrf_token": client.cookies.get("dash_csrf"), "next": "",
        })

    unknown = attempt("no-such-user", "whatever-long-pass")
    wrong = attempt(LOGIN, "wrong-password-entirely")
    assert unknown.status_code == wrong.status_code == 401
    assert "Неверный логин или пароль" in unknown.text
    assert "Неверный логин или пароль" in wrong.text


def test_login_without_csrf_token_is_rejected(client):
    """Без двойной отправки чужая страница могла бы залогинить пользователя."""
    response = client.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD, "next": "",
    })
    assert response.status_code == 400
    assert "dash_session" not in response.cookies


def test_brute_force_is_locked_out(client, dash_app):
    for _ in range(dash_app.state.config.login_max_attempts):
        client.get(f"{BASE}/login")
        client.post(f"{BASE}/login", data={
            "username": LOGIN, "password": "wrong-password-entirely",
            "csrf_token": client.cookies.get("dash_csrf"), "next": "",
        })
    client.get(f"{BASE}/login")
    response = client.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD,
        "csrf_token": client.cookies.get("dash_csrf"), "next": "",
    })
    # Даже верный пароль не проходит, пока окно блокировки не истекло.
    assert response.status_code == 429


def test_disabled_user_cannot_log_in(client):
    store.set_user_active(LOGIN, False)
    client.get(f"{BASE}/login")
    response = client.post(f"{BASE}/login", data={
        "username": LOGIN, "password": PASSWORD,
        "csrf_token": client.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 401


def test_revoked_session_stops_working(auth_client):
    """Серверные сессии нужны именно ради этого: отзыв доступа мгновенный."""
    assert auth_client.get(f"{BASE}/today").status_code == 200
    store.revoke_all_sessions(LOGIN)
    assert auth_client.get(f"{BASE}/today").status_code == 303


def test_forged_session_cookie_is_rejected(client):
    client.cookies.set("dash_session", "not-a-signed-value", path=BASE)
    assert client.get(f"{BASE}/").status_code == 303


# --------------------------------------------------------------------------
# куки, заголовки, редиректы
# --------------------------------------------------------------------------

def test_session_cookie_carries_every_protective_flag():
    """Флаги куки проверяются напрямую: по http клиент Secure-куку не сохранит."""
    from fastapi.responses import Response

    from security import issue_session_cookie

    config = SecurityConfig(base_path=BASE, secret_key="z" * 48, cookie_secure=True)
    response = Response()
    issue_session_cookie(response, "test-sid", config)
    cookie = response.headers["set-cookie"]

    assert "HttpOnly" in cookie, "без HttpOnly куку читает любой скрипт на странице"
    assert "Secure" in cookie, "без Secure куку видно в незашифрованном канале"
    assert "samesite=lax" in cookie.lower()
    assert f"Path={BASE}" in cookie
    # Значение подписано: подставить чужой идентификатор сессии нельзя.
    assert cookie.split("dash_session=")[1].split(";")[0] != "test-sid"


def test_logout_clears_the_session_cookie(auth_client):
    header = auth_client.get(f"{BASE}/logout").headers.get("set-cookie", "")
    assert "dash_session=" in header
    assert 'dash_session=""' in header or "Max-Age=0" in header or "expires=" in header.lower()


def test_security_headers_are_present_even_on_denial(client):
    response = client.get(f"{BASE}/")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_authenticated_pages_are_not_cached(auth_client):
    """Иначе кнопка «назад» после выхода показывает данные из кеша браузера."""
    response = auth_client.get(f"{BASE}/")
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("target,expected", [
    ("//evil.example", f"{BASE}/"),
    ("https://evil.example/x", f"{BASE}/"),
    ("/other/path", f"{BASE}/"),
    ("/\\evil.example", f"{BASE}/"),
    (f"{BASE}/deals?period=7d", f"{BASE}/deals?period=7d"),
    ("", f"{BASE}/"),
])
def test_next_parameter_cannot_redirect_offsite(target, expected):
    """Иначе ссылка на наш домен уводила бы сотрудника на чужую форму входа."""
    assert safe_next(target, BASE) == expected


def test_missing_secret_key_refuses_to_start(monkeypatch):
    """Генерировать ключ на старте нельзя: рестарт молча разлогинивал бы всех."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="DASHBOARD_SECRET_KEY"):
        SecurityConfig.from_settings(get_settings())


# --------------------------------------------------------------------------
# токен вебхука не должен попасть в HTML
# --------------------------------------------------------------------------

def test_crm_link_never_leaks_the_webhook_token():
    """Всё после /rest/ — это ключ от всей CRM. В HTML он попасть не может."""
    webhook = "https://b24-example.bitrix24.ru/rest/1/super-secret-token/"
    link = crm_link("deal", 42, webhook)
    assert link == "https://b24-example.bitrix24.ru/crm/deal/details/42/"
    assert "super-secret-token" not in link
    assert "/rest/" not in link
    assert portal_domain(webhook) == "https://b24-example.bitrix24.ru"


def test_rendered_pages_do_not_contain_the_webhook_token(auth_client):
    settings = get_settings()
    token = settings.b24_webhook_url.split("/rest/")[-1].strip("/")
    for path in (f"{BASE}/", f"{BASE}/table", f"{BASE}/quality"):
        body = auth_client.get(path).text
        assert token not in body, f"токен вебхука утёк в {path}"
        assert "/rest/" not in body


def test_no_template_uses_an_inline_style_attribute():
    """Строгий CSP их всё равно не применит — молча сломается вёрстка.

    style-src 'self' блокирует style="" в разметке. Ослабить политику ради
    отступа значит открыть подмену интерфейса через внедрённую разметку,
    поэтому статика живёт в классах, а динамика ставится из JS через CSSOM.
    """
    import re
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"
    offenders = [
        f"{path.name}: {match}"
        for path in templates.glob("*.html")
        for match in re.findall(r'style="[^"]*"', path.read_text(encoding="utf-8"))
    ]
    assert not offenders, offenders


def test_csp_forbids_inline_styles_and_scripts(client):
    policy = client.get(f"{BASE}/login").headers["content-security-policy"]
    assert "style-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
