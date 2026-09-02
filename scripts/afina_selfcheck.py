"""Где рвётся связь дашборда с Афиной CRM.

Запускать там, где живёт дашборд, — иначе проверка ничего не доказывает:
`curl` с ноутбука доходит до Афины и тогда, когда контейнер до неё не
достучится.

    docker compose exec dashboard python scripts/afina_selfcheck.py

Шаги идут от простого к сложному и обрываются на первом сломанном: имя,
сеть, сертификат, ключ. Номер последнего напечатанного шага и есть ответ,
что чинить.

Адрес можно передать аргументом — перебрать кандидатов, не правя .env и не
перезапуская контейнер:

    docker compose exec dashboard python scripts/afina_selfcheck.py \
        http://172.17.0.1:8001
"""
import os
import socket
import ssl
import sys
from urllib.parse import urlsplit

sys.path.insert(0, "src")
import httpx  # noqa: E402

DASHBOARD_PATH = "/api/public/dashboard/summary"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def _settings(argv: list[str]) -> tuple[str, str]:
    """Адрес и ключ так, как их видит сам дашборд.

    Читаем из окружения, а не из Settings: внутри контейнера важно ровно то,
    что туда доехало через env_file, а не то, что лежит в .env рядом. Адрес
    из аргумента перебивает окружение — так подбирают рабочий адрес, не
    перезапуская контейнер на каждую попытку.
    """
    override = argv[1] if len(argv) > 1 else ""
    return (
        (override or os.environ.get("AFINA_API_BASE_URL") or "").rstrip("/"),
        (os.environ.get("AFINA_API_KEY") or "").strip(),
    )


def main(argv: list[str]) -> int:
    base, key = _settings(argv)
    print(f"1. адрес: {base or '(ПУСТО)'}")
    print(f"   ключ: {'задан, ' + str(len(key)) + ' симв.' if key else '(ПУСТО)'}")
    if not base or not key:
        print("   → раздела «Объекты» не будет, пока оба не заданы")
        return 1
    for var in PROXY_VARS:
        if os.environ.get(var):
            print(f"   прокси в окружении: {var}={os.environ[var]}")

    parts = urlsplit(base)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        print("   → адрес не разбирается, ожидается вид https://afina-crm.ru")
        return 1
    if parts.path.strip("/"):
        # Клиент дописывает /api/public/dashboard/... сам, поэтому путь в
        # адресе склеивается в двойной и даёт 404 на пятом шаге. Не ошибка:
        # Афина может стоять за прокси с префиксом. Но чаще это вставленный
        # целиком эндпоинт, и увидеть это надо здесь, а не гадать по 404.
        print(f"   ВНИМАНИЕ: в адресе есть путь /{parts.path.strip('/')}.")
        print("   Ожидается корень сервиса; путь витрины клиент дописывает сам")

    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except OSError as exc:
        print(f"2. DNS {host}: НЕ РЕЗОЛВИТСЯ ({exc})")
        print("   → имя не известно контейнеру: опечатка в адресе или DNS")
        return 1
    print(f"2. DNS {host}: {', '.join(addresses)}")

    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError as exc:
        print(f"3. TCP {host}:{port}: НЕ ОТКРЫВАЕТСЯ ({exc})")
        print("   → до адреса не достучаться именно отсюда. Если Афина на этом")
        print("     же сервере, укажите её внутренний адрес, а не публичный")
        return 1
    print(f"3. TCP {host}:{port}: открыт")

    if parts.scheme == "https":
        try:
            with socket.create_connection((host, port), timeout=5) as raw:
                context = ssl.create_default_context()
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    print(f"4. TLS: принят, {tls.version()}")
        except (OSError, ssl.SSLError) as exc:
            print(f"4. TLS: НЕ ПРИНЯТ ({exc})")
            print("   → цепочка сертификатов или разъехавшееся время на сервере")
            return 1

    url = f"{base}{DASHBOARD_PATH}"
    print(f"5. запрос: GET {url}")
    try:
        response = httpx.get(url, headers={"X-API-Key": key}, timeout=10)
    except httpx.HTTPError as exc:
        print(f"   не дошёл: {type(exc).__name__}")
        return 1
    print(f"   HTTP {response.status_code}: {response.text[:200]}")
    if response.status_code == 200:
        print("   → связь рабочая, раздел «Объекты» должен наполняться")
        return 0
    print({
        401: "   → ключ не совпал с PUBLIC_API_KEY на стороне Афины",
        403: "   → ключ не совпал с PUBLIC_API_KEY на стороне Афины",
        404: ("   → витрины по этому адресу нет. Три причины: адрес ведёт на\n"
              "     другой сервис, в адресе лишний путь, бэкенд не пересобран"),
        503: "   → на стороне Афины не задан PUBLIC_API_KEY",
    }.get(response.status_code, "   → неожиданный ответ, смотрите тело выше"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
