"""Уведомление, которое обязано быть замеченным, и обычное сообщение — разное.

Оба канала у портала есть, и разница между ними не косметическая. Сообщение
в личный чат человек может не открыть неделю: чат — это лента, и в ней
тонет всё. Пометка «важное» поднимает сообщение наверх, а уведомление
(im.notify.personal.add) висит на колокольчике, пока его не прочитают.

Эти параметры восстановлены в репозитории из правки, которая жила только на
боевом сервере: `send_user_chat_message` принимала `important`, а модуль,
вызывающий её с `important=True`, лежал там же неотслеживаемым. При
очередном развёртывании параметр исчез из образа, и вызов сломался бы с
TypeError — молча, в задаче, которую запускают руками раз в день. Тест
здесь затем, чтобы второй раз этого не случилось.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import notify  # noqa: E402

USER = 32


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, dict]]:
    """Перехват вызовов портала: что за метод и с какими параметрами."""
    calls: list[tuple[str, dict]] = []

    def _call(method: str, params: dict):
        calls.append((method, params))
        return 777

    monkeypatch.setattr(notify, "_bx_call_sync", _call)
    return calls


# ── Важное сообщение ───────────────────────────────────────────────────
def test_an_ordinary_message_carries_no_importance_flag(sent):
    """По умолчанию ничего не меняется: флага в запросе нет вовсе."""
    notify.send_user_chat_message(USER, "обычное")

    _, params = sent[0]
    assert "IMPORTANT" not in params
    assert "PARAMS" not in params


def test_an_important_message_says_so_in_both_places(sent):
    """Оба ключа, потому что какой сработает — зависит от версии портала.

    IMPORTANT — флаг мессенджера, PARAMS.IS_IMPORTANT — тот же флаг на самом
    сообщении. Неизвестный ключ REST игнорирует, так что лишний безвреден, а
    недостающий стоил бы незамеченного уведомления.
    """
    notify.send_user_chat_message(USER, "важное", important=True)

    method, params = sent[0]
    assert method == "im.message.add"
    assert params["IMPORTANT"] == "Y"
    assert params["PARAMS"] == {"IS_IMPORTANT": "Y"}


def test_importance_is_keyword_only():
    """Позиционно его передать нельзя — иначе спутают с system."""
    with pytest.raises(TypeError):
        notify.send_user_chat_message(USER, "текст", True, True)


# ── Длинное важное сообщение ───────────────────────────────────────────
def test_every_chunk_of_a_long_message_stays_important(sent):
    """Разрезали на куски — важность не должна остаться в первом.

    Иначе человек видит поднятый наверх обрывок и хвост где-то в ленте.
    """
    long_text = "\n".join(f"строка {i}" for i in range(400))
    chunks = notify.send_user_chat_message_chunked(
        USER, long_text, chunk_size=200, important=True,
    )

    assert chunks > 1
    assert len(sent) == chunks
    assert all(params["IMPORTANT"] == "Y" for _, params in sent)


def test_a_short_message_goes_in_one_piece_and_keeps_the_flag(sent):
    assert notify.send_user_chat_message_chunked(
        USER, "коротко", important=True,
    ) == 1
    assert sent[0][1]["IMPORTANT"] == "Y"


def test_chunked_messages_are_ordinary_by_default(sent):
    notify.send_user_chat_message_chunked(USER, "коротко")

    assert "IMPORTANT" not in sent[0][1]


# ── Уведомление на колокольчик ─────────────────────────────────────────
def test_a_personal_notify_uses_the_notification_method(sent):
    notify_id = notify.send_personal_notify(USER, "посмотрите карточку")

    method, params = sent[0]
    assert method == "im.notify.personal.add"
    assert params == {"USER_ID": USER, "MESSAGE": "посмотрите карточку"}
    assert notify_id == 777


def test_a_notify_without_a_recipient_is_skipped_not_sent(sent):
    """Ноль — это «получателя не нашли», и слать такое некуда."""
    assert notify.send_personal_notify(0, "текст") == 0
    assert sent == []
