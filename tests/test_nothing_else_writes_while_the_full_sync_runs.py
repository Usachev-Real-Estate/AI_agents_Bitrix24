"""В витрину пишет кто-то один: SQLite в WAL второго писателя не пускает.

20.09.2026 полная сверка и очередной тик инкремента стартовали в одну минуту
(02:30 UTC). Обёртка scripts/cron_job.sh берёт flock по ИМЕНИ задачи, а имена
были разные — analytics-etl и analytics-full, — так что каждая защищала себя
только от самой себя. Дальше решал SQLite: busy_timeout 10 секунд, полная
сверка держит запись куда дольше, и инкременты падали с «database is locked»
в 02:30, 02:45 и 03:00.

Следа от этих падений не осталось даже на странице «Качество данных»: журнал
прогонов пишется отдельным соединением ровно затем, чтобы упавший прогон был
виден, — но упиралось в тот же замок и оно.

Тем же вечером выяснилось, что читатель комментариев (он пишет в ту же
витрину) стоял в 03:00 UTC, то есть в середине сверки, — и не падал только
потому, что раньше кончались деньги у модели. В комментарии крона при этом
значилось, что сверка идёт в 03:30 МСК: ошибка пересчёта UTC в МСК на два
часа, прожившая в файле с момента появления задачи.

Отсюда и этот тест. Расписание — текстовый файл, который правят руками и
порознь; здесь проверяется не стиль, а три вещи, ломающиеся молча и по ночам:
общий замок у обоих режимов ETL, отсутствие гонки за него и запас времени у
всех остальных, кто пишет в витрину.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# `<расписание> cd … scripts/cron_job.sh <имя> docker run … python <путь>`
_ENTRY = re.compile(
    r"^(?P<sched>\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+.*?"
    r"cron_job\.sh\s+(?P<job>\S+)\s+.*?"
    r"\bpython3?\s+(?P<script>\S+\.py)",
    re.MULTILINE,
)

# Запас перед полной сверкой для тех, кто не под её замком. Час — это худшая
# ночь читателя комментариев (20 минут) с четырёхкратным резервом.
RUNWAY_MIN = 60

ETL = "src/analytics/etl.py"
# Точки входа крона, которые ПИШУТ в витрину. Это `grep -l analytics_session`
# по путям из crontab.txt; полнота списка проверяется отдельным тестом ниже.
SHOPFRONT_WRITERS = {ETL, "src/comment_reader.py"}


def _entries() -> list[dict[str, str]]:
    text = (_ROOT / "crontab.txt").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return [m.groupdict() for m in _ENTRY.finditer("\n".join(lines))]


def _minutes(sched: str) -> set[int]:
    """Минуты, в которые срабатывает расписание. Понимает `*`, `*/N` и число."""
    field = sched.split()[0]
    if field == "*":
        return set(range(60))
    if field.startswith("*/"):
        step = int(field[2:])
        return set(range(0, 60, step))
    return {int(part) for part in field.split(",")}


def _start(sched: str) -> int:
    """Минуты от полуночи. Только для задач с конкретным часом и минутой."""
    minute, hour = sched.split()[0], sched.split()[1]
    return int(hour) * 60 + int(minute)


def _by_script(script: str) -> list[dict[str, str]]:
    return [e for e in _entries() if e["script"] == script]


# ── Сначала — что разбор вообще работает ───────────────────────────────
def test_the_scan_actually_finds_the_cron_jobs():
    """Пустой разбор прошёл бы «успешно», ничего не проверив.

    Регулярка по чужому формату — самое ломкое место теста: строку крона
    перепишут, разбор вернёт ноль записей, и все проверки ниже станут
    тавтологией, продолжая зеленеть.
    """
    entries = _entries()

    # Сейчас их 19: считаются только строки вида `… python <файл>`, а аудит
    # и чистка Docker запускаются иначе.
    assert len(entries) >= 15
    assert len(_by_script(ETL)) == 2, "ожидаются два режима ETL: инкремент и сверка"
    assert {e["script"] for e in entries} >= SHOPFRONT_WRITERS


# ── Общий замок у обоих режимов ETL ────────────────────────────────────
def test_both_etl_modes_share_one_lock():
    """Разные имена задач — разные файлы блокировки, то есть её отсутствие.

    flock в scripts/cron_job.sh берётся на /tmp/b24-auditor-<имя>.lock. Пока
    имена различались, ничто не мешало сверке и инкременту писать разом.
    """
    names = {e["job"] for e in _by_script(ETL)}

    assert len(names) == 1, f"режимы ETL разъехались по замкам: {sorted(names)}"


def test_the_full_sync_does_not_race_the_incremental_for_the_lock():
    """Общий замок без разведения по минутам меняет одну беду на другую.

    Обе задачи стартовали в 02:30. С общим именем замок достаётся тому, кто
    успел первым, — и раз через раз пропускалась бы уже полная сверка, молча
    и с нулевым кодом возврата. А она единственная, кто помечает удалённые
    карточки: без неё удалённая сделка навсегда остаётся в счётчиках воронки.
    """
    full = next(e for e in _by_script(ETL) if e["sched"].split()[1] != "*")
    tick = next(e for e in _by_script(ETL) if e["sched"].split()[1] == "*")

    assert _minutes(full["sched"]) & _minutes(tick["sched"]) == set()


# ── Остальные писатели витрины ─────────────────────────────────────────
def test_every_other_writer_starts_well_before_the_full_sync():
    """Запас нужен именно ДО сверки: когда она кончится — неизвестно.

    Длительность полной сверки ничем не ограничена и зависит от портфеля:
    20.09 она шла больше получаса. Поэтому проверяется то, что проверить
    можно, — что задача успевает отработать до её начала, а не догадка о
    том, что сверка к такому-то часу уже закончилась.
    """
    full_at = _start(next(
        e for e in _by_script(ETL) if e["sched"].split()[1] != "*"
    )["sched"])
    etl_lock = next(iter({e["job"] for e in _by_script(ETL)}))

    for entry in _entries():
        if entry["script"] not in SHOPFRONT_WRITERS or entry["script"] == ETL:
            continue
        if entry["job"] == etl_lock:
            continue  # под тем же замком — разводить по времени не нужно
        runway = full_at - _start(entry["sched"])
        assert runway >= RUNWAY_MIN, (
            f"{entry['script']} стартует за {runway} мин до полной сверки"
        )


def test_the_list_of_writers_is_still_complete():
    """Список писателей — это grep, а не память.

    Следующую задачу, которая пишет в витрину, добавят так же, как добавили
    читателя комментариев: строкой в crontab.txt. Тест должен заметить её
    сам, а не ждать ночного «database is locked».
    """
    found = set()
    for entry in _entries():
        path = _ROOT / entry["script"]
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        if "analytics_session" in source or "init_analytics_db" in source:
            found.add(entry["script"])

    assert found == SHOPFRONT_WRITERS, (
        "в крон добавился писатель витрины — впишите его в SHOPFRONT_WRITERS "
        "и убедитесь, что он не попадает в окно полной сверки"
    )
