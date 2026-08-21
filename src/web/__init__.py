"""Веб-дашборд аналитики Bitrix24.

src/ — плоский каталог модулей, а не пакет: точки входа проекта кладут его на
sys.path и импортируют плоско (см. src/main.py:11-13). Повторяем это, чтобы
`from config import ...` работало и при запуске `python src/web/server.py`,
и при импорте из тестов.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "analytics")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
