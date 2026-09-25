"""Разбор клиента моделью: кого берём, что пишем и чего не пишем.

Каждый разбор стоит денег, и главный способ их потратить зря — заново
пересказать вчерашний вывод теми же словами. Поэтому повторно берутся
только те, у кого с прошлого разбора что-то произошло.

Второе: разбор не переписывается, а дописывается. `client_reviews` —
единственная таблица книги, которую нельзя пересобрать из витрины: её
написала модель или человек.

Третье: ответ, который не читается, в таблицу не попадает вовсе. Строка
без summary выглядит на карточке как «модель посмотрела и ничего не
нашла», хотя на деле она ответила не тем форматом.
"""

import json

import pytest

from clients import review as review_mod
from clients.review import SCOPE, VERDICTS, Review, parse, pending, run
from clients.schema import clients_session, init_clients_db
from config import get_settings

ANSWER = {
    "summary": "Клиент просил перезвонить после майских, никто не перезвонил.",
    "verdict": "нужен звонок",
    "issues": ["обещали перезвонить и не перезвонили"],
    "recommendation": "Позвонить и предложить два варианта из подбора",
}


class _Model:
    """Модель, отвечающая заранее заготовленным. Сети не касается."""

    def __init__(self, *answers):
        self.answers = list(answers) or [json.dumps(ANSWER, ensure_ascii=False)]
        self.seen = []

    def invoke(self, messages):
        self.seen.append(messages)
        content = self.answers[min(len(self.seen) - 1, len(self.answers) - 1)]
        if isinstance(content, Exception):
            raise content
        return type("Answer", (), {"content": content, "response_metadata": {}})()


@pytest.fixture
def book(tmp_path, monkeypatch):
    path = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(path))
    init_clients_db(path)
    # Кэш расшифровок подменяется на пустой: тест про выбор и запись, а не
    # про тексты, и открывать настоящую data/violations.db он не должен.
    monkeypatch.setattr(review_mod, "texts_of", lambda ids: {})
    return path


def _client(conn, key, *, state="cooling", last_event="2026-09-01T10:00:00+00:00",
            silence=10, talks=0):
    conn.execute(
        "INSERT INTO clients(client_key, triage_state, last_event_at, silence_days,"
        " calls_with_transcript, updated_at) VALUES (?, ?, ?, ?, ?, '')",
        (key, state, last_event, silence, talks),
    )


def _card(conn, key, entity_id, *, stage="C18:NEW", closed=0):
    conn.execute(
        "INSERT INTO client_links(client_key, entity_type, entity_id, stage_id,"
        " closed) VALUES (?, 'deal', ?, ?, ?)", (key, entity_id, stage, closed),
    )


def _review(conn, key, *, through, summary="прошлый вывод"):
    conn.execute(
        "INSERT INTO client_reviews(client_key, created_at, reviewed_through,"
        " summary, author) VALUES (?, '2026-09-02T00:00:00+00:00', ?, ?, 'модель')",
        (key, through, summary),
    )


def _rows(book, sql, params=()):
    with clients_session(book, readonly=True) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


# ── кого берём ────────────────────────────────────────────────────────

def test_only_the_states_that_need_a_decision_are_reviewed(book):
    """Закрытых разбирать нечего, отказавшихся незачем, «в работе» идёт."""
    with clients_session(book) as conn:
        for number, state in enumerate(
                (*SCOPE, "closed", "refused", "moving", "no_data"), start=1):
            _client(conn, f"p:+7900000000{number}", state=state)

    with clients_session(book, readonly=True) as conn:
        assert len(pending(conn)) == len(SCOPE)


def test_a_client_nothing_happened_to_is_not_reviewed_twice(book):
    """Главный способ потратить деньги зря — пересказать вчерашнее."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event="2026-09-01T10:00:00+00:00")
        _review(conn, "p:+79001112233", through="2026-09-01T10:00:00+00:00")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == []


def test_a_client_something_happened_to_comes_back(book):
    """Событие после разбора — повод пересмотреть вывод."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event="2026-09-10T10:00:00+00:00")
        _review(conn, "p:+79001112233", through="2026-09-01T10:00:00+00:00")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79001112233"]


def test_a_client_without_a_single_event_is_reviewed(book):
    """«Завели и забыли» — самый повод для разбора, а не причина пропустить.

    Сравнивать не с чем: событий нет вовсе. Пропустив таких, разбор обошёл
    бы стороной ровно тех, ради кого список и заводили.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event=None)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79001112233"]


def test_a_client_with_no_events_is_reviewed_once_and_not_every_night(book):
    """Разобрали пустого клиента — второй раз он не придёт.

    У него ничего не происходит по определению, и пересматривать нечего.
    Без этой проверки условие «берём, если событий нет» выглядит
    страховкой от пустоты, а работает как подписка: каждую ночь, вечно,
    за деньги, с одним и тем же выводом.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event=None)
        _review(conn, "p:+79001112233", through="")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == []


def test_the_first_event_of_an_empty_client_brings_him_back(book):
    """Появилось событие — сравнение оживает само: пусто меньше любой даты."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event="2026-09-10T10:00:00+00:00")
        _review(conn, "p:+79001112233", through="")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79001112233"]


def test_the_longest_silence_goes_first(book):
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", silence=5)
        _client(conn, "p:+79000000002", silence=90)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn)[0] == "p:+79000000002"


def test_the_budget_caps_who_is_taken(book):
    with clients_session(book) as conn:
        for number in range(1, 6):
            _client(conn, f"p:+7900000000{number}", silence=number)

    summary = run(llm=_Model(), budget=2)

    assert summary["к разбору"] == 2
    assert len(_rows(book, "SELECT * FROM client_reviews")) == 2, "остальные ждут ночи"


def test_a_dry_run_names_the_whole_queue_and_not_just_its_slice(book):
    """«К разбору 150» при очереди в семьсот читается как «их всего 150».

    По такому отчёту нельзя понять, разгребается очередь или стоит на
    месте, — а это единственный вопрос, ради которого сухой прогон и
    запускают.
    """
    with clients_session(book) as conn:
        for number in range(1, 8):
            _client(conn, f"p:+7900000000{number}", silence=number)

    summary = run(llm=_Model(), dry_run=True, budget=3)

    assert summary["в очереди"] == 7
    assert summary["к разбору"] == 3


def test_the_clients_with_something_to_read_go_first(book):
    """Порядок стоил боевого прогона: пять «данных мало» подряд.

    Очередь шла по одной тишине, и первые разборы достались карточкам без
    единого разговора. Вывод по такой карточке уже сделан правилом, и
    пересказывать его моделью — платить за имитацию разбора.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", silence=90, talks=0)
        _client(conn, "p:+79000000002", silence=5, talks=3)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79000000002", "p:+79000000001"]


def test_an_empty_card_is_moved_back_and_not_thrown_out(book):
    """Дойдёт бюджет — разберём и их, но после тех, где есть слова."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", talks=0)
        _client(conn, "p:+79000000002", talks=None)
        _client(conn, "p:+79000000003", talks=2)

    with clients_session(book, readonly=True) as conn:
        found = pending(conn)

    assert found[0] == "p:+79000000003"
    assert set(found) == {"p:+79000000001", "p:+79000000002", "p:+79000000003"}


def test_the_longest_silence_still_wins_among_equals(book):
    """Внутри группы с материалом порядок прежний."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", silence=5, talks=1)
        _client(conn, "p:+79000000002", silence=90, talks=1)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn)[0] == "p:+79000000002"


# ── что записывается ──────────────────────────────────────────────────

def test_a_review_is_added_and_the_old_one_stays(book):
    """Таблицу разборов нельзя пересобрать — только дописать."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233", last_event="2026-09-10T10:00:00+00:00")
        _review(conn, "p:+79001112233", through="2026-09-01T10:00:00+00:00",
                summary="прошлый вывод")

    run(llm=_Model(), budget=None)

    rows = _rows(book, "SELECT * FROM client_reviews ORDER BY id")
    assert len(rows) == 2, "прошлый разбор остаётся историей"
    assert rows[0]["summary"] == "прошлый вывод"
    assert rows[1]["summary"] == ANSWER["summary"]
    assert rows[1]["reviewed_through"] == "2026-09-10T10:00:00+00:00"
    assert json.loads(rows[1]["issues_json"]) == ANSWER["issues"]
    assert rows[1]["author"] == "модель"


def test_an_unreadable_answer_is_not_written_at_all(book):
    """Строка без вывода читается как «посмотрели и ничего не нашли»."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")

    summary = run(llm=_Model("извините, не могу"), budget=None)

    assert _rows(book, "SELECT * FROM client_reviews") == []
    assert summary["не ответила"] == 1 and summary["разобрано"] == 0


def test_a_model_that_raises_costs_one_client_not_the_run(book):
    """Один недоступный ответ стоит одного клиента, а не ночи."""
    with clients_session(book) as conn:
        _client(conn, "p:+79000000001", silence=90)
        _client(conn, "p:+79000000002", silence=5)

    summary = run(llm=_Model(RuntimeError("таймаут"),
                             json.dumps(ANSWER, ensure_ascii=False)), budget=None)

    assert summary == {**summary, "разобрано": 1, "не ответила": 1}
    assert len(_rows(book, "SELECT * FROM client_reviews")) == 1


def test_enough_data_is_ours_and_not_the_models(book):
    """Спросив модель, много ли у неё данных, мы спросим заинтересованного.

    У клиента нет ни разговоров, ни человеческих записей — значит данных
    мало, что бы модель о себе ни написала.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")

    answer = dict(ANSWER, enough_data=True)
    run(llm=_Model(json.dumps(answer, ensure_ascii=False)), budget=None)

    assert _rows(book, "SELECT enough_data FROM client_reviews")[0]["enough_data"] == 0


def test_a_dry_run_asks_nobody_and_writes_nothing(book):
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")

    model = _Model()
    summary = run(llm=model, dry_run=True, budget=None)

    assert summary["к разбору"] == 1 and summary["сухой прогон"] is True
    assert model.seen == [] and _rows(book, "SELECT * FROM client_reviews") == []


# ── ответ модели ──────────────────────────────────────────────────────

def test_a_verdict_from_the_list_is_kept_as_written():
    assert parse(json.dumps({"summary": "с", "verdict": VERDICTS[0]})).verdict == VERDICTS[0]


def test_a_verdict_of_its_own_invention_is_kept_and_counted(book):
    """Выбросить — потерять след расхождения, подставить — соврать."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")

    answer = dict(ANSWER, verdict="надо бы позвонить")
    summary = run(llm=_Model(json.dumps(answer, ensure_ascii=False)), budget=None)

    assert summary["вердикт вне словаря"] == 1
    assert _rows(book, "SELECT verdict FROM client_reviews")[0]["verdict"] == "надо бы позвонить"


def test_the_answer_survives_a_fence_around_it():
    """Модели любят обернуть JSON в ```json — это не повод терять разбор."""
    fenced = "```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"

    assert parse(fenced).summary == ANSWER["summary"]


def test_no_more_than_five_issues_reach_the_card():
    answer = {"summary": "с", "issues": [f"проблема {n}" for n in range(9)]}

    assert len(parse(json.dumps(answer, ensure_ascii=False)).issues) == 5


def test_issues_that_are_not_a_list_are_simply_absent():
    assert parse(json.dumps({"summary": "с", "issues": "строка"})).issues == ()


def test_an_empty_review_is_not_a_review():
    assert parse(json.dumps({"verdict": VERDICTS[0]})) is None
    assert parse("вообще не json") is None
    assert Review().summary == ""


# ── стадии, которые разбор не трогает ─────────────────────────────────
#
# «Продавцы / Поиск клиента»: карточка живёт там до появления покупателя,
# и работа идёт с объектом, а не с человеком. Пропускается ЭТАП, а не
# человек — это и есть главное, что здесь проверяется.

SKIP = ("C20:SEARCH",)


def test_a_client_who_only_sits_on_a_skipped_stage_is_left_alone(book):
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="C20:SEARCH")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn, skip_stages=SKIP) == []


def test_a_live_deal_elsewhere_brings_the_client_back(book):
    """Пропускается этап, а не человек.

    У продавца рядом бывает сделка покупателя, и молчание по ней —
    полноценный повод для разбора. Выбросив клиента целиком, мы потеряли
    бы ровно ту половину, ради которой список и заводили.
    """
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="C20:SEARCH")
        _card(conn, "p:+79001112233", 2, stage="C18:NEW")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn, skip_stages=SKIP) == ["p:+79001112233"]


def test_a_closed_card_elsewhere_does_not_bring_him_back(book):
    """Закрытая карточка — не повод разбирать: работы по ней больше нет."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="C20:SEARCH")
        _card(conn, "p:+79001112233", 2, stage="C18:WON", closed=1)

    with clients_session(book, readonly=True) as conn:
        assert pending(conn, skip_stages=SKIP) == []


def test_a_client_without_cards_stays_in_the_queue(book):
    """Вне списка у него быть нечему, и без оговорки он выпал бы заодно."""
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn, skip_stages=SKIP) == ["p:+79001112233"]


def test_without_a_skip_list_nobody_is_skipped(book):
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="C20:SEARCH")

    with clients_session(book, readonly=True) as conn:
        assert pending(conn) == ["p:+79001112233"]


def test_the_sellers_search_stage_is_skipped_out_of_the_box(book, monkeypatch):
    """Умолчание — боевой идентификатор, а не пустота.

    Значение портальное и непрозрачное, и держать его только в `.env`
    значит однажды выкатиться без него и молча вернуть в очередь триста
    пятьдесят карточек, которые разбирать не просили.
    """
    monkeypatch.delenv("CLIENTS_REVIEW_SKIP_STAGES_JSON", raising=False)
    get_settings.cache_clear()
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="UC_FADPBF")

    summary = run(llm=_Model(), dry_run=True, budget=None)
    get_settings.cache_clear()

    assert summary["в очереди"] == 0


def test_the_run_takes_the_skip_list_from_the_settings(book, monkeypatch):
    """Список живёт в настройке: этап меняют в портале, а не в коде."""
    monkeypatch.setenv("CLIENTS_REVIEW_SKIP_STAGES_JSON", '["C20:SEARCH"]')
    get_settings.cache_clear()
    with clients_session(book) as conn:
        _client(conn, "p:+79001112233")
        _card(conn, "p:+79001112233", 1, stage="C20:SEARCH")

    summary = run(llm=_Model(), dry_run=True, budget=None)
    get_settings.cache_clear()

    assert summary["в очереди"] == 0
    assert summary["пропускаем стадии"] == ["C20:SEARCH"]
