"""Токен вебхука не попадает в лог ни на каком уровне.

httpx пишет на уровне INFO строку на каждый запрос — вместе с полным
адресом. В адресе вебхука Битрикса лежит токен, а запросов у нас порядка
полутораста на тик при тике раз в пятнадцать минут. Это пятнадцать тысяч
копий боевого доступа к CRM в сутки, в файле, который лежит рядом с кодом,
уезжает в любую пересылку лога и однажды уже дорастал до 14 ГБ.

Утечка тихая вдвойне: строка выглядит как обычная отладка, и заметить её
можно только прочитав лог глазами — что однажды и произошло.

Правило живёт в одном месте, в `quiet_http_clients()`, и зовётся из
`setup_logging()`. Здесь проверяется не уровень логгера, а следствие: что
строка с токеном действительно никуда не доходит.
"""

import logging
import re
from pathlib import Path

import pytest

from config import quiet_http_clients, setup_logging

_ROOT = Path(__file__).resolve().parent.parent

# Похоже на боевой адрес и содержит то, что терять нельзя.
WEBHOOK = "https://b24-example.bitrix24.ru/rest/154/s3cr3ttok3n/crm.deal.list"
REQUEST_LINE = f'HTTP Request: POST {WEBHOOK} "HTTP/1.1 200 OK"'

# Точки входа крона, которым HTTP не нужен вовсе. Список именной: молчаливое
# исключение по маске однажды накроет задачу, которая в портал ходит.
NO_HTTP = {"src/web/manage.py"}

_ENTRY = re.compile(r"python (src/[\w/]+\.py)")


class _Sink(logging.Handler):
    """Всё, что дошло до обработчиков корня."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def logging_restored():
    """Вернуть логи как были: setup_logging сносит обработчики корня."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, saved in quiet.items():
        logging.getLogger(name).setLevel(saved)


def _reaches_the_log(level: str, name: str, method: str, message: str,
                     tmp_path) -> list[str]:
    """Что дойдёт до обработчиков, если такой-то логгер такое-то напишет."""
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)          # setup_logging заводит ./logs — не в репозитории
    try:
        setup_logging(level)
    finally:
        os.chdir(cwd)
    sink = _Sink()
    logging.getLogger().addHandler(sink)
    try:
        getattr(logging.getLogger(name), method)(message)
    finally:
        logging.getLogger().removeHandler(sink)
    return sink.lines


@pytest.mark.parametrize("level", ["INFO", "DEBUG"], ids=["обычный", "отладка"])
@pytest.mark.parametrize("name", ["httpx", "httpcore"])
def test_a_request_line_with_the_token_never_reaches_the_log(
    level, name, tmp_path, logging_restored,
):
    """Строка запроса с адресом вебхука до лога не доходит.

    DEBUG проверяется отдельно и не для порядка: именно его включают, когда
    что-то не работает и лог начинают читать. Правило, которое отключается
    ровно в этот момент, — не правило.
    """
    assert _reaches_the_log(level, name, "info", REQUEST_LINE, tmp_path) == []


def test_a_portal_failure_still_gets_through(tmp_path, logging_restored):
    """Гасится шум, а не ошибки: WARNING от клиента остаётся в логе.

    Обратная половина. Без неё те же тесты прошли бы и для правила
    «выключить логи HTTP совсем», а тогда упавший портал стал бы невидим.
    """
    got = _reaches_the_log("INFO", "httpx", "warning", "портал не ответил", tmp_path)

    assert got == ["портал не ответил"]


def test_our_own_lines_are_not_touched(tmp_path, logging_restored):
    """Свои сообщения задача по-прежнему пишет как писала."""
    got = _reaches_the_log("INFO", "analytics.etl", "info", "Готово: 608 комментариев",
                           tmp_path)

    assert got == ["Готово: 608 комментариев"]


def test_the_rule_works_without_setup_logging(logging_restored):
    """quiet_http_clients() самодостаточна.

    Три задачи крона настраивают лог через basicConfig и setup_logging не
    зовут. Им нужна именно эта функция отдельно, и она обязана работать
    сама по себе.
    """
    logging.getLogger("httpx").setLevel(logging.INFO)
    quiet_http_clients()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_every_cron_entry_point_quiets_the_portal_client():
    """Ни одна задача крона не остаётся с говорливым клиентом.

    Это и есть то, что делает правку долговечной. Следующую задачу добавят
    строкой в crontab.txt, и если она настроит лог по-своему — а так уже
    сделали три, — токен потечёт снова. Тест ловит это при добавлении, а не
    через месяц чтением лога.
    """
    text = (_ROOT / "crontab.txt").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    scripts = sorted({m.group(1) for m in _ENTRY.finditer("\n".join(lines))})
    assert scripts, "в crontab.txt не нашлось ни одной точки входа — сломан разбор"

    noisy = []
    for script in scripts:
        path = _ROOT / script
        if script in NO_HTTP or not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        # Ищется ВЫЗОВ, а не упоминание. Первая редакция этого теста грепала
        # имя и потому была слепа: строка `from config import ...,
        # quiet_http_clients` удовлетворяла проверке у задачи, которая
        # функцию импортировала и не звала.
        if not any(call in source for call in ("setup_logging(", "quiet_http_clients(")):
            noisy.append(script)

    assert noisy == [], (
        f"задачи крона не глушат клиента портала: {noisy}. "
        "Позовите quiet_http_clients() — в адресе вебхука лежит токен"
    )


def test_the_exception_list_is_not_a_place_to_hide(tmp_path):
    """Исключение оправдано, только пока задача действительно не ходит в сеть."""
    for script in NO_HTTP:
        source = (_ROOT / script).read_text(encoding="utf-8")
        for marker in ("httpx", "import notify", "from notify", "BitrixClient"):
            assert marker not in source, (
                f"{script} обращается к сети ({marker}) — ему не место в NO_HTTP"
            )
