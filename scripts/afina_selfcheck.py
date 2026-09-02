"""Где рвётся связь дашборда с Афиной CRM.

Запускать там, где живёт дашборд, — иначе проверка ничего не доказывает:
`curl` с ноутбука доходит до Афины и тогда, когда контейнер до неё не
достучится.

    docker compose exec dashboard python scripts/afina_selfcheck.py

Шаги идут от простого к сложному и обрываются на первом сломанном: имя,
сеть, сертификат, ключ. Номер последнего напечатанного шага и есть ответ,
что чинить.
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


def _settings() -> tuple[str, str]:
    """Адрес и ключ так, как их видит сам дашборд.

    Читаем из окружения, а не из Settings: внутри контейнера важно ровно то,
    что туда доехало через env_file, а не то, что лежит в .env рядом.
    """
    return (
        (os.environ.get("AFINA_API_BASE_URL") or "").rstrip("/"),
        (os.environ.get("AFINA_API_KEY") or "").strip(),
    )


def main() -> int:
    base, key = _settings()
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

    try:
        response = httpx.get(
            f"{base}{DASHBOARD_PATH}", headers={"X-API-Key": key}, timeout=10,
        )
    except httpx.HTTPError as exc:
        print(f"5. запрос не дошёл: {type(exc).__name__}")
        return 1
    print(f"5. HTTP {response.status_code}: {response.text[:200]}")
    if response.status_code == 200:
        print("   → связь рабочая, раздел «Объекты» должен наполняться")
        return 0
    print({
        401: "   → ключ не совпал с PUBLIC_API_KEY на стороне Афины",
        403: "   → ключ не совпал с PUBLIC_API_KEY на стороне Афины",
        404: "   → на этом адресе нет витрины: бэкенд Афины не пересобран",
        503: "   → на стороне Афины не задан PUBLIC_API_KEY",
    }.get(response.status_code, "   → неожиданный ответ, смотрите тело выше"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
