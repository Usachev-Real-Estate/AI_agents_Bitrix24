"""Выписка для модели: что ей дают и что честно признают скрытым.

У клиента с сорока звонками история в запрос не влезет, и подрезать её
придётся. Молча обрезанная история — худшее из возможного: модель
уверенно ответит про то, чего не читала, и отличить такой ответ от
честного нельзя ничем. Поэтому проверяется не только то, что выписка
собирается, но и что она вслух называет пропущенное.

Второе по важности — `enough_data`. Считаем его мы, а не модель: спросив
её саму, много ли у неё данных, мы получили бы оценку от того, кто
заинтересован ответить уверенно.
"""

from clients.brief import (
    TIMELINE_HEAD, TIMELINE_LIMIT, TRANSCRIPT_CHARS, TRANSCRIPT_LIMIT,
    TRANSCRIPT_ONE_CHARS, FeedEvent, build,
)

CLIENT = {"name": "Сидоров Пётр", "triage_state": "cooling", "silence_days": 12,
          "phone_norm": "+79001112233", "phone_raw": "+7 900 111-22-33 (жена)"}


def _call(number: int, *, day: int = 1, direction: int = 1, seconds: int = 120):
    at = f"2026-09-{day:02d}T10:00:00+00:00"
    end = f"2026-09-{day:02d}T10:{seconds // 60:02d}:00+00:00"
    return FeedEvent(at=at, kind="call", source_id=str(number),
                     payload={"direction": direction, "start_time": at, "end_time": end})


def _note(number: int, body: str, *, day: int = 1, is_system: bool = False):
    return FeedEvent(at=f"2026-09-{day:02d}T11:00:00+00:00", kind="comment",
                     source_id=f"c{number}", payload={"body": body},
                     is_system=is_system)


# ── что уходит наружу ─────────────────────────────────────────────────

def test_the_phone_number_does_not_leave_with_the_brief():
    """Чтобы сказать, что с клиентом, номер не нужен.

    Выписка уходит в чужой сервис. Имя остаётся — без него модель не
    отличит клиента от брокера в расшифровке, — а номер не добавляет к
    ответу ничего.
    """
    found = build(CLIENT, [], [], {})

    assert found.client["name"] == "Сидоров Пётр"
    assert "phone_norm" not in found.client
    assert "phone_raw" not in found.client


def test_a_card_gives_only_the_fields_the_model_needs():
    cards = [{"title": "2-к на Ленина", "stage_name": "Подбор", "closed": 0,
              "stage_id": "C18:NEW", "date_create": "2026-01-10",
              "client_key": "p:+79001112233", "category_id": 18}]

    found = build(CLIENT, cards, [], {})

    assert found.cards[0]["title"] == "2-к на Ленина"
    assert "client_key" not in found.cards[0], "ключ модели ни о чём не говорит"
    # Признак закрытия обязан доехать: без него модель выводит его из
    # названия стадии, а там встречается «Закрытая продажа» — способ
    # продажи, а не состояние сделки. Восемьдесят восемь таких карточек
    # в боевой книге, и все они в работе.
    assert found.cards[0]["closed"] == 0


# ── подрезка ленты ────────────────────────────────────────────────────

def test_a_short_feed_is_shown_whole_and_nothing_is_claimed_hidden():
    events = [_note(number, f"запись {number}", day=number) for number in range(1, 6)]

    found = build(CLIENT, [], events, {})

    assert len(found.timeline) == 5
    assert not found.trimmed, "скрывать было нечего"


def test_a_long_feed_keeps_both_ends_and_says_how_much_it_dropped():
    """Края важнее середины: с чего началось и где стоит сейчас.

    Середина предсказуема и пересказывается по краям; начало отвечает на
    вопрос «чего человек хотел», без него свежий хвост читается как
    разговор с незнакомцем.
    """
    events = [_note(number, f"запись {number}", day=(number % 28) + 1)
              for number in range(1, TIMELINE_LIMIT + 11)]

    found = build(CLIENT, [], events, {})

    assert len(found.timeline) == TIMELINE_LIMIT
    assert found.trimmed.events == 10
    ordered = sorted(events, key=lambda event: (event.at, event.source_id))
    shown = {entry["текст"] for entry in found.timeline}
    assert shown & {event.payload["body"] for event in ordered[:TIMELINE_HEAD]}
    assert shown & {event.payload["body"] for event in ordered[-3:]}


def test_the_timeline_runs_forward_in_time():
    """Историю читают от начала. Лента задом наперёд читается как другая."""
    events = [_note(2, "позже", day=20), _note(1, "раньше", day=2)]

    found = build(CLIENT, [], events, {})

    assert [entry["текст"] for entry in found.timeline] == ["раньше", "позже"]


def test_a_call_direction_is_spelled_out_in_words():
    """«1» и «2» модель прочтёт как попало, а разница решающая."""
    found = build(CLIENT, [], [_call(1, direction=1), _call(2, direction=2)], {})

    assert {entry["направление"] for entry in found.timeline} == {"входящий", "исходящий"}


def test_a_robot_record_is_marked_as_a_robot():
    """Слово «отказ» в шаблоне интеграции — не слова клиента."""
    found = build(CLIENT, [], [_note(1, "Статус изменён", is_system=True)], {})

    assert found.timeline[0]["робот"] is True


# ── разговоры ─────────────────────────────────────────────────────────

def test_the_newest_conversations_come_first():
    """Решение принимают по последнему разговору, а не по первому."""
    events = [_call(1, day=1), _call(2, day=20)]

    found = build(CLIENT, [], events, {1: "старый разговор", 2: "свежий разговор"})

    assert [talk["текст"] for talk in found.talks] == ["свежий разговор", "старый разговор"]


def test_a_call_without_a_transcript_is_not_a_conversation():
    found = build(CLIENT, [], [_call(1), _call(2)], {1: "есть текст"})

    assert len(found.talks) == 1
    assert found.talks[0]["текст"] == "есть текст"


def test_too_many_conversations_are_counted_not_silently_dropped():
    events = [_call(number, day=number) for number in range(1, TRANSCRIPT_LIMIT + 4)]
    texts = {number: "разговор" for number in range(1, TRANSCRIPT_LIMIT + 4)}

    found = build(CLIENT, [], events, texts)

    assert len(found.talks) == TRANSCRIPT_LIMIT
    assert found.trimmed.transcripts == 3


def test_one_endless_conversation_does_not_eat_the_whole_budget():
    """Часовой звонок целиком не оставил бы места остальным."""
    events = [_call(1, day=20), _call(2, day=1)]
    texts = {1: "я" * (TRANSCRIPT_ONE_CHARS * 2), 2: "второй разговор"}

    found = build(CLIENT, [], events, texts)

    assert len(found.talks[0]["текст"]) == TRANSCRIPT_ONE_CHARS
    assert found.trimmed.cut_texts == 1
    assert [talk["текст"] for talk in found.talks][1] == "второй разговор"


def test_the_whole_budget_is_respected_across_conversations():
    events = [_call(number, day=number) for number in range(1, 6)]
    texts = {number: "я" * TRANSCRIPT_ONE_CHARS for number in range(1, 6)}

    found = build(CLIENT, [], events, texts)

    assert sum(len(talk["текст"]) for talk in found.talks) <= TRANSCRIPT_CHARS


# ── есть ли на чём отвечать ───────────────────────────────────────────

def test_a_client_with_a_conversation_has_something_to_answer_from():
    assert build(CLIENT, [], [_call(1)], {1: "разговор"}).enough_data is True


def test_a_human_note_is_enough_even_without_conversations():
    assert build(CLIENT, [], [_note(1, "клиент просил не звонить до мая")], {}).enough_data is True


def test_dates_alone_are_not_enough():
    """Ни разговора, ни человеческой записи — остаются одни даты.

    Разбор при этом делается: «завели и ни разу не позвонили» — вывод, и
    ценный. Но помечен он должен быть так, чтобы брокер увидел это раньше,
    чем прочтёт вывод.
    """
    events = [_call(1), _note(2, "Стадия изменена роботом", is_system=True)]

    assert build(CLIENT, [], events, {}).enough_data is False


def test_an_empty_note_is_not_a_note():
    assert build(CLIENT, [], [_note(1, "   ")], {}).enough_data is False


# ── то, что уходит в запрос ───────────────────────────────────────────

def test_the_payload_names_what_was_hidden():
    """Подсказка велит модели оговорить пропуски — значит их надо назвать."""
    events = [_note(number, f"запись {number}", day=(number % 28) + 1)
              for number in range(1, TIMELINE_LIMIT + 6)]

    payload = build(CLIENT, [], events, {}).as_payload()

    assert payload["чего не показано"]["событий скрыто"] == 5
    assert payload["есть на чём отвечать"] is True
    assert set(payload) == {"клиент", "карточки", "лента", "разговоры",
                            "чего не показано", "есть на чём отвечать"}
