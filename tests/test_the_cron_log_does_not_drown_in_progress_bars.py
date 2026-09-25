"""Выгрузка не имеет права засорять общий лог построчным отчётом о запросах.

fast_bitrix24 на каждый запрос пишет строку INFO и рисует прогрессбар.
Полная выгрузка досье — это около двух с половиной тысяч запросов по
портфелю в 1289 карточек, то есть столько же пар строк, уходящих в общий
logs/cron.log вместе с выводом всех остальных задач.

Это не придирка к аккуратности. На боевом сервере этот файл однажды дорос
до 14 ГБ при диске в 41 и положил всё разом — ETL, аудит и дашборд;
предохранитель в scripts/cron_job.sh появился именно после того случая.
Ежедневная задача, добавляющая туда мегабайт строк «Starting get_all», —
это шаг ровно в ту сторону.

Проверка нужна потому, что выключатель легко потерять при следующей правке
клиента: пропажа не роняет ничего, лог просто снова начинает расти, и
замечают это через месяцы.

Выключатель один на всех и живёт рядом с флагом, в tools. Задач, которым
он нужен, теперь две — выгрузка досье и догрузка расшифровок, — и у каждой
проверено, что она его зовёт.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import dossier  # noqa: E402
import tools  # noqa: E402
from clients import pull_transcripts  # noqa: E402


@pytest.fixture(autouse=True)
def restore_globals(monkeypatch):
    """Флаг и уровень логгера — глобальные: вернуть их после теста.

    monkeypatch запоминает исходные значения сам, поэтому соседние тесты не
    получат в наследство тихий клиент.
    """
    monkeypatch.setattr(tools, "BX_VERBOSE", True)
    logger = logging.getLogger("fast_bitrix24")
    level = logger.level
    yield
    logger.setLevel(level)


def test_the_switch_turns_the_progress_bars_off():
    tools.quiet_the_portal_client()

    assert tools.BX_VERBOSE is False


def test_the_switch_turns_the_per_request_lines_off():
    tools.quiet_the_portal_client()

    assert logging.getLogger("fast_bitrix24").level == logging.WARNING


def test_portal_errors_still_reach_the_log():
    """Гасится шум, а не диагностика. WARNING остаётся видимым."""
    tools.quiet_the_portal_client()

    assert logging.getLogger("fast_bitrix24").isEnabledFor(logging.WARNING)
    assert logging.getLogger("fast_bitrix24").isEnabledFor(logging.ERROR)


def test_the_flag_actually_reaches_the_client():
    """Выключатель без провода — это выключатель, который ничего не гасит.

    Клиент собирается здесь по-настоящему: конструктор в сеть не ходит, а
    проверить надо именно то, что значение доезжает до него.
    """
    tools.BX_VERBOSE = False
    assert tools._get_bitrix().verbose is False

    tools.BX_VERBOSE = True
    assert tools._get_bitrix().verbose is True


def test_other_jobs_keep_their_progress_bars():
    """По умолчанию ничего не меняется: аудит запускают руками и смотрят.

    Тихим клиент становится только там, где задача его об этом попросила.
    """
    assert tools.BX_VERBOSE is True
    assert tools._get_bitrix().verbose is True


def test_the_transcript_backfill_turns_them_off_too(monkeypatch):
    """Догрузка расшифровок — вторая задача с тем же прогрессбаром.

    До четырёхсот запросов за ночь, по паре строк на каждый. Проверяется
    не сам выключатель — он проверен выше, — а что задача его зовёт:
    пропажа этой строки ничего не уронит, и заметят её по размеру лога
    через месяцы.

    Работа подменена: гасить клиент надо ДО первого запроса, и тест об
    этом не узнал бы, если бы ждал конца настоящего прогона.
    """
    monkeypatch.setattr(pull_transcripts, "pull", lambda **kwargs: {})
    monkeypatch.setattr(sys, "argv", ["pull_transcripts", "--dry-run"])

    assert pull_transcripts.main() == 0
    assert tools.BX_VERBOSE is False
    assert logging.getLogger("fast_bitrix24").level == logging.WARNING


def test_the_dossier_export_turns_them_off_too(monkeypatch):
    """Задача, ради которой выключатель и заводили.

    Две с половиной тысячи запросов за полный прогон. Проверка была на
    сам выключатель, но не на то, что выгрузка его зовёт, — и переживала
    бы его пропажу молча.

    Выгрузка подменена целиком: она ходит в портал и пишет на диск, а
    проверить надо один вызов, который делается до всего этого.
    """
    monkeypatch.setattr(dossier, "run", lambda *args, **kwargs: None)

    assert dossier.main([]) == 0
    assert tools.BX_VERBOSE is False
    assert logging.getLogger("fast_bitrix24").level == logging.WARNING
