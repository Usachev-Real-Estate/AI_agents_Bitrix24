"""get_all() листает страницы сам, и указывать ему страницу нельзя.

`fast_bitrix24.get_all()` проверяет это контрактом и падает с
`icontract.errors.ViolationError`, если в параметрах оказались `order` или
`start`. Падает — на портале, в первом же боевом запуске.

Поймать это заранее нечем: в тестах запросы замоканы, и параметры никто не
смотрит, поэтому код с лишним `order` проходит и `flake8`, и весь набор
тестов, и ломается ровно там, где проверить дороже всего. Так и случилось с
разведкой звонков: параметр переехал из старой версии скрипта, где
пагинация делалась вручную через `call()`, которому он разрешён.

Поэтому проверка здесь не на результат, а на то, ЧТО УШЛО В ЗАПРОС.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dossier  # noqa: E402
import probe_transcripts  # noqa: E402

# Ровно те ключи, которые запрещает контракт get_all(). Регистр значения не
# имеет: fast_bitrix24 приводит имена параметров к верхнему.
FORBIDDEN = {"ORDER", "START"}


class _Recorder:
    """Заглушка get_all, запоминающая каждый запрос."""

    def __init__(self, answer: object | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer if answer is not None else []

    def __call__(self, method: str, params: dict) -> object:
        self.calls.append((method, params))
        return self.answer

    def forbidden_keys(self) -> set[str]:
        found: set[str] = set()
        for _, params in self.calls:
            found |= {k.upper() for k in params} & FORBIDDEN
        return found


@pytest.fixture
def probe_calls(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(probe_transcripts, "_bx_get_all_sync", recorder)
    return recorder


@pytest.fixture
def dossier_calls(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(dossier, "_bx_get_all_sync", recorder)
    return recorder


# ── Разведка ───────────────────────────────────────────────────────────
def test_the_probe_does_not_order_the_deal_list(probe_calls):
    """Тот самый упавший запрос."""
    probe_transcripts.list_open_deals([18, 0], limit=0)

    assert probe_calls.calls, "запрос вообще не ушёл"
    assert probe_calls.forbidden_keys() == set()


def test_the_probe_does_not_order_the_activity_list(probe_calls):
    probe_transcripts.list_call_activities([1, 2, 3])

    assert probe_calls.forbidden_keys() == set()


def test_the_probe_still_takes_the_newest_deals_first(probe_calls):
    """Сортировка ушла к нам, но «свежие N» для --limit обязаны остаться."""
    probe_calls.answer = [
        {"ID": "10"}, {"ID": "300"}, {"ID": "200"},
    ]

    deals = probe_transcripts.list_open_deals([18], limit=2)

    assert [d["ID"] for d in deals] == ["300", "200"]


# ── Выгрузка ───────────────────────────────────────────────────────────
def test_the_dossier_does_not_order_the_deal_list(dossier_calls):
    dossier.list_deals(18, "UF_CRM_TEST")

    assert dossier_calls.calls, "запрос вообще не ушёл"
    assert dossier_calls.forbidden_keys() == set()


def test_the_dossier_does_not_order_the_assignee_sweep(dossier_calls):
    dossier.list_assignees([18, 0])

    assert dossier_calls.forbidden_keys() == set()


def test_the_dossier_does_not_order_the_user_directory(dossier_calls):
    dossier.load_users()

    assert dossier_calls.forbidden_keys() == set()


# ── Что запрос всё-таки несёт ──────────────────────────────────────────
def test_the_afina_field_is_asked_for_by_its_discovered_code(dossier_calls):
    """Проверка заодно: поле Афины попадает в select, а не теряется."""
    dossier.list_deals(18, "UF_CRM_1780911079")

    _, params = dossier_calls.calls[0]
    assert "UF_CRM_1780911079" in params["select"]


def test_closed_deals_stay_out_unless_asked_for(dossier_calls):
    dossier.list_deals(18, "")
    _, params = dossier_calls.calls[0]
    assert params["filter"]["CLOSED"] == "N"

    dossier_calls.calls.clear()
    dossier.list_deals(18, "", include_closed=True)
    _, params = dossier_calls.calls[0]
    assert "CLOSED" not in params["filter"]
