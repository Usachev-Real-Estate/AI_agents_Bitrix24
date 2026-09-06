"""Готовая строка AFINA_DEPARTMENT_MAP_JSON для раздела «Объекты».

Пока перевод не задан, РОП раздела не видит: справочники Битрикса и Афины по
имени не сходятся — «Трофимова» против «Отдел Трофимовой», — а подбирать пару
автоматически значило бы угадывать падеж. Ошибка здесь показала бы РОПу чужой
отдел, поэтому пары называют руками. Скрипт делает руками только это: обе
стороны он печатает сам.

Запускать там, где живёт дашборд: витрина и ключ Афины есть только там.

    docker compose exec dashboard python scripts/afina_departments.py
"""
import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "src/web")
sys.path.insert(0, "src/analytics")

import objects  # noqa: E402
from afina import AfinaClient, AfinaError  # noqa: E402
from config import get_settings  # noqa: E402
from metrics import departments_options  # noqa: E402
from scope import Scope, scoped_session  # noqa: E402


def _bitrix() -> list[dict]:
    """Отделы Битрикса — те же, что в выпадающем списке дашборда.

    Scope.everything(): скрипт запускает администратор на сервере, и урезанный
    список отделов сделал бы карту неполной, а не безопасной.
    """
    with scoped_session(Scope.everything()) as conn:
        return departments_options(conn)


def _afina(settings) -> list[str]:
    """Отделы Афины — те, что реально встречаются в рабочих объектах."""
    client = AfinaClient(
        settings.afina_api_base_url,
        settings.afina_api_key,
        settings.afina_api_timeout_seconds,
    )
    objects.reset_cache()
    return objects.departments_of(objects.snapshot(client)["items"])


def main() -> int:
    settings = get_settings()
    if not objects.is_configured(settings):
        print("Связь с Афиной не настроена: нужны AFINA_API_KEY и AFINA_API_BASE_URL")
        return 1

    try:
        afina = _afina(settings)
    except AfinaError as exc:
        print(f"Афина не ответила: {exc}")
        print("Сначала scripts/afina_selfcheck.py — он покажет, что именно сломано")
        return 1

    print("Отделы Афины (правая часть пары):")
    for name in afina:
        print(f"  {name}")

    print("\nОтделы Битрикса (левая часть пары):")
    bitrix = _bitrix()
    for row in bitrix:
        print(f"  {row['department_id']:>4}  {row['name']}")

    # Заготовка, а не готовая настройка: сопоставить пары может только
    # человек, который знает, какой отдел Афины чей. Пустые значения видно
    # сразу, и подставить в них имя из списка выше — минутное дело.
    draft = {str(row["department_id"]): "" for row in bitrix}
    print("\nЗаготовка для .env — впишите названия из верхнего списка:")
    print("AFINA_DEPARTMENT_MAP_JSON="
          + json.dumps(draft, ensure_ascii=False, separators=(", ", ": ")))
    print("\nОтдел без пары остаётся без раздела: его РОП «Объекты» не увидит.")

    current = settings.afina_department_map
    if current:
        print(f"\nСейчас задано: {json.dumps(current, ensure_ascii=False)}")
        unknown = sorted(set(current.values()) - set(afina))
        if unknown:
            # Молчаливая опечатка здесь выглядит как «у РОПа пустой раздел»,
            # и искать её будут где угодно, только не в .env.
            print("ВНИМАНИЕ: таких отделов в Афине нет — "
                  + ", ".join(f"«{name}»" for name in unknown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
