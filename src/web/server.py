"""Точка входа веб-сервиса: python src/web/server.py

Слушает только localhost. Наружу дашборд выставляет nginx, он же терминирует
TLS. Прямой публичный порт означал бы дашборд в интернете без шифрования.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    _HERE = Path(__file__).resolve().parent
    for _p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "analytics")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from app import create_app  # noqa: E402
from config import get_settings, setup_logging  # noqa: E402


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)

    import uvicorn

    try:
        application = create_app(settings)
    except RuntimeError as exc:
        print(f"Дашборд не запущен: {exc}", file=sys.stderr)
        return 1

    uvicorn.run(
        application,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level=settings.log_level.lower(),
        # Заголовки прокси разбирает наш код (security.client_ip) и только для
        # запросов с localhost. Доверять им на уровне uvicorn нельзя: тогда
        # адрес клиента подделывался бы заголовком.
        proxy_headers=False,
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
