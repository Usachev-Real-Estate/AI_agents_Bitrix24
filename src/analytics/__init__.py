"""Аналитическая витрина: ETL из Bitrix24 + метрики для дашборда.

src/ — плоский каталог модулей, а не пакет: точки входа проекта кладут его на
sys.path и импортируют плоско (см. src/main.py:11-13). Повторяем это здесь,
чтобы `from config import ...` и `from client import ...` работали одинаково
и при запуске `python src/analytics/etl.py`, и при импорте из тестов.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE), str(_HERE.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
