"""Первый проход по портфелю должен влезать в ночь и не стоить лишнего.

Замер на боевом портале: карточка отвечает секунд за тридцать пять. Тысяча
карточек — десять часов, то есть первый проход не влезает ни в какую ночь,
и никакая смена модели этого не чинит: втрое быстрее нужной модели не
бывает. Зато карточки друг от друга не зависят — значит лечится не
выбором модели, а тем, чтобы не ждать по одной.

Второе. Читатель звал make_llm(settings) без аргументов и потому не брал
из .env ни тариф, ни закреплённого провайдера — при том что аудитор берёт
оба. Тариф — это ровно половина счёта. Провайдер — это неявный кэш, а у
читателя постоянная часть запроса есть ВЕСЬ промпт: ни одна другая наша
задача так не выигрывает от прогретого префикса и так не проигрывает от
того, что запросы раскиданы по провайдерам.

Третье. Спор о том, дорого ли это и не сменить ли модель, до сих пор
решался голосованием: прогон не сообщал ни времени, ни рублей, ни доли
размышлений. Теперь сообщает — и спор решается делением.
"""

from __future__ import annotations

import logging
import threading
from datetime import date

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path

import comment_reader
from schema import analytics_session

ANSWER = '{"promised": "Позвонить в пятницу", "promised_at": "2026-08-15"}'


class _Tariff:
    """Настройки боевого .env: flex у закреплённого провайдера."""

    llm_api_key = "k"
    llm_base_url = "https://example/api/v1"
    llm_model = "google/gemini-3.7-flash"
    llm_max_tokens = 0
    llm_reasoning_effort = ""
    llm_service_tier = "flex"
    llm_provider = "google-ai-studio"
    llm_price_input = 100.0
    llm_price_output = 400.0
    llm_price_cache_read = 10.0
    llm_price_cache_write = 25.0


class _Answer:
    def __init__(self, content=ANSWER, usage=None):
        self.content = content
        self.usage_metadata = usage or {}


def _deal(conn, deal_id):
    conn.execute(
        """
        INSERT INTO fact_deal(deal_id, title, category_id, stage_id,
            assigned_by_id, source_id, opportunity, currency_id, date_create,
            date_modify, closedate, is_closed, is_won, is_lost, contact_id,
            is_deleted, synced_at)
        VALUES (?, 'ВГ 747', 0, 'UC_FADPBF', 10, 'ADV', 0, 'RUB',
                '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00',
                NULL, 0, 0, 0, 5000, 0, 'x')
        """,
        (deal_id,),
    )
    conn.execute(
        "INSERT INTO fact_comment(comment_id, entity_type, entity_id,"
        " author_id, body, is_auto, created_at, synced_at)"
        " VALUES (?, 'deal', ?, 10, 'Позвонить в пятницу', 0,"
        " '2026-08-10T10:00:00+00:00', 'x')",
        (deal_id * 100, deal_id),
    )


@pytest.fixture
def mart(analytics_db):
    with analytics_session() as conn:
        for deal_id in (1, 2, 3, 4):
            _deal(conn, deal_id)
    return analytics_db


def _read(model, **kwargs):
    with analytics_session() as conn:
        return comment_reader.read_cards(
            conn, model, today=date(2026, 9, 9), **kwargs,
        )


# ── Тариф и провайдер из настроек ──────────────────────────────────────
def test_the_reader_pays_the_same_price_as_the_auditor():
    """Дешёвый режим и закреплённый провайдер берутся из .env, а не теряются.

    Голый make_llm(settings) означал «обычная цена, любой провайдер» —
    вдвое дороже и мимо кэша, причём молча.
    """
    model, spare = comment_reader._clients(_Tariff())

    assert model.extra_body == {
        "provider": {"only": ["google-ai-studio/flex"], "allow_fallbacks": False},
    }
    # Запасной — тот же провайдер, обычный тариф: у него прогрет префикс,
    # и терять кэш из-за отказа по мощностям было бы обидно вдвойне.
    assert spare.extra_body == {
        "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
    }


def test_without_a_cheap_tier_there_is_nothing_to_fall_back_to():
    """Тариф не задан — запасной клиент не нужен: повторять не с чего."""
    plain = _Tariff()
    plain.llm_service_tier = ""

    model, spare = comment_reader._clients(plain)

    assert spare is None
    assert model.extra_body == {
        "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
    }


# ── Отказ дешёвого режима ──────────────────────────────────────────────
def test_a_refusal_on_the_cheap_tier_is_retried_on_the_normal_one(mart):
    """flex обещает отказать при нехватке мощностей. Это не повод терять карточку."""
    class _Busy:
        def invoke(self, messages):
            raise RuntimeError("no capacity")

    class _Normal:
        def __init__(self):
            self.calls = 0

        def invoke(self, messages):
            self.calls += 1
            return _Answer()

    spare = _Normal()
    assert _read(_Busy(), spare=spare, workers=1) == 4
    assert spare.calls == 4


def test_with_no_spare_a_refusal_costs_one_card_and_not_the_batch(mart):
    """Без запасного клиента отказ пропускает карточку, а не роняет партию."""
    class _Flaky:
        def __init__(self):
            self.seen = 0

        def invoke(self, messages):
            self.seen += 1
            if self.seen == 1:
                raise RuntimeError("no capacity")
            return _Answer()

    assert _read(_Flaky(), workers=1) == 3


# ── Одновременность ────────────────────────────────────────────────────
def test_the_cards_are_asked_at_the_same_time(mart):
    """Четыре карточки уходят в модель одновременно, а не по очереди.

    Барьер пропускает, только когда в нём собрались все четверо. Читай
    читатель по одной, первый же вызов ждал бы остальных до таймаута,
    барьер сломался бы и карточки остались непрочитанными.
    """
    gate = threading.Barrier(4, timeout=10)

    class _Slow:
        def invoke(self, messages):
            gate.wait()
            return _Answer()

    assert _read(_Slow(), workers=4) == 4


def test_the_cards_are_written_once_each(mart):
    """Читаем вчетвером, пишем по одной: ни потерь, ни дублей."""
    class _Model:
        def invoke(self, messages):
            return _Answer()

    assert _read(_Model(), workers=4) == 4
    with analytics_session() as conn:
        rows = conn.execute(
            "SELECT entity_id, COUNT(*) FROM fact_comment_read"
            " GROUP BY entity_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1, 1), (2, 1), (3, 1), (4, 1)]


# ── Счёт ───────────────────────────────────────────────────────────────
def test_the_run_counts_what_it_spent(mart):
    """Токены суммируются по всем карточкам партии."""
    usage_one = {
        "input_tokens": 2_500, "output_tokens": 200,
        "input_token_details": {"cache_read": 2_000},
        "output_token_details": {"reasoning": 120},
    }

    class _Model:
        def invoke(self, messages):
            return _Answer(usage=usage_one)

    usage = {key: 0 for key in comment_reader.USAGE_KEYS}
    _read(_Model(), workers=4, usage=usage)

    assert usage["input_tokens"] == 10_000
    assert usage["cached_tokens"] == 8_000
    assert usage["reasoning_tokens"] == 480


def test_an_unreadable_answer_is_still_paid_for(mart):
    """За нечитаемый ответ уже заплачено — прятать его из счёта нельзя.

    Иначе цена прогона занижается ровно на самых неудачных карточках, то
    есть тем сильнее, чем хуже дела.
    """
    class _Mumbling:
        def invoke(self, messages):
            return _Answer(content="я подумаю",
                           usage={"input_tokens": 2_500, "output_tokens": 200})

    usage = {key: 0 for key in comment_reader.USAGE_KEYS}
    assert _read(_Mumbling(), workers=4, usage=usage) == 0
    assert usage["input_tokens"] == 10_000


def test_the_run_reports_time_and_money(caplog):
    """Итог прогона: секунды на карточку, рубли и доля размышлений.

    Это те три числа, по которым решают, менять ли модель. Пока их не
    печатали, вопрос решался голосованием.
    """
    usage = {
        "input_tokens": 10_000, "output_tokens": 1_000,
        "cached_tokens": 8_000, "reasoning_tokens": 580,
        "cache_write_tokens": 0,
    }
    with caplog.at_level(logging.INFO, logger=comment_reader.__name__):
        comment_reader._report(20, usage, _Tariff(), seconds=140.0)

    line = caplog.text
    assert "20 карточек за 140 с (7.0 с/карточка)" in line
    # 2000 свежих × 100 + 8000 × 10 + 1000 × 400 = 0,68 ₽ на миллион.
    assert "0.68 ₽ (0.034 ₽/карточка)" in line
    assert "8000 из кэша, 80%" in line
    assert "580 размышления, 58%" in line


def test_an_empty_run_does_not_divide_by_zero(caplog):
    """Нечего читать — итог всё равно печатается, а не падает."""
    usage = {key: 0 for key in comment_reader.USAGE_KEYS}
    with caplog.at_level(logging.INFO, logger=comment_reader.__name__):
        comment_reader._report(0, usage, _Tariff(), seconds=0.4)

    assert "0 карточек" in caplog.text


# ── Куда уходят токены ─────────────────────────────────────────────────
def test_the_raw_answer_can_be_looked_at(mart, monkeypatch, capsys):
    """Сырой ответ модели рядом с его ценой.

    Счёт говорит: 640 токенов выхода на карточку при ответе строк на
    восемьдесят. Разница либо размышления, которых провайдер не разделяет,
    либо пояснения вокруг JSON, которых мы просили не писать. Первое
    лечится только сменой модели, второе — строкой промпта, и различить их
    можно единственным способом: посмотреть, что пришло.
    """
    class _Chatty:
        def invoke(self, messages):
            return _Answer(
                content="Разбираю запись.\n" + ANSWER,
                usage={"input_tokens": 1_500, "output_tokens": 640,
                       "output_token_details": {"reasoning": 0}},
            )

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Chatty(), None))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--raw", "1"])

    assert comment_reader.main() == 0
    out = capsys.readouterr().out
    assert "выход 640 токенов, из них размышления 0" in out
    assert "Разбираю запись." in out, "пояснение модели должно быть видно"


def test_the_probe_writes_nothing(mart, monkeypatch, capsys):
    """Проба состояния не меняет: посмотреть и записать — разные действия."""
    class _Model:
        def invoke(self, messages):
            return _Answer()

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Model(), None))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--raw", "2"])
    comment_reader.main()

    with analytics_session() as conn:
        read = conn.execute("SELECT COUNT(*) FROM fact_comment_read").fetchone()[0]
    assert read == 0


# ── Провал должен быть слышен ──────────────────────────────────────────
def test_a_run_that_read_nothing_fails_loudly(mart, monkeypatch):
    """Модель отказала на всех карточках — это авария, а не пустая ночь.

    По крону такой прогон возвращал ноль и молчал: советы неделю опирались
    бы на устаревший разбор, и заметить это можно было бы только по тому,
    что утренняя сводка перестала называть новые карточки. Ненулевой код
    поднимает штатный алерт.
    """
    class _Dead:
        def invoke(self, messages):
            raise RuntimeError("нет ответа")

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Dead(), None))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--limit", "4"])

    assert comment_reader.main() == 1


def test_an_empty_queue_is_not_a_failure(mart, monkeypatch):
    """Читать было нечего — это нормальная ночь, а не повод будить админа."""
    class _Model:
        def invoke(self, messages):
            return _Answer()

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Model(), None))
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--limit", "4"])
    assert comment_reader.main() == 0

    # Второй прогон подряд: всё уже прочитано, очередь пуста.
    assert comment_reader.main() == 0


def test_the_leftover_queue_is_reported(mart, monkeypatch, caplog):
    """Остаток очереди виден в логе — иначе узкое место не отличить от нормы.

    Прочитано ровно столько, сколько разрешено, — прогон выглядит удачным,
    а разбор отстаёт на день, потом на два, и советы тихо стареют. Замер
    на живом портале: за два часа рабочего дня очередь набрала 72
    карточки, то есть упереться в лимит — не гипотеза.
    """
    class _Model:
        def invoke(self, messages):
            return _Answer()

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Model(), None))
    monkeypatch.setattr(comment_reader, "setup_logging", lambda level: None)
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--limit", "1"])

    with caplog.at_level(logging.WARNING, logger=comment_reader.__name__):
        assert comment_reader.main() == 0

    assert "В очереди осталось 3 карточек" in caplog.text


def test_a_drained_queue_says_nothing(mart, monkeypatch, caplog):
    """Всё прочитано — молчим: предупреждение о пустом остатке это шум."""
    class _Model:
        def invoke(self, messages):
            return _Answer()

    monkeypatch.setattr(comment_reader, "_clients",
                        lambda settings: (_Model(), None))
    monkeypatch.setattr(comment_reader, "setup_logging", lambda level: None)
    monkeypatch.setattr("sys.argv", ["comment_reader.py", "--limit", "10"])

    with caplog.at_level(logging.WARNING, logger=comment_reader.__name__):
        assert comment_reader.main() == 0

    assert "В очереди осталось" not in caplog.text


# ── Прочитанное не пропадает ───────────────────────────────────────────
def test_each_card_is_saved_where_it_was_read(mart):
    """Прогон идёт полчаса по чужой сети, и обрыв не должен стоить партии.

    Транзакция открыта на весь сеанс: пока карточки писались одним
    коммитом, обрыв на тысячной откатывал всю тысячу — тысяча оплаченных
    ответов исчезала, потому что тысяча первый не состоялся. Заодно
    блокировка записи держалась все тридцать две минуты прогона, и ETL,
    тикнувший в середине, упал с «database is locked».
    """
    class _Model:
        def invoke(self, messages):
            return _Answer()

    with analytics_session() as conn:
        assert comment_reader.read_cards(
            conn, _Model(), today=date(2026, 9, 9), workers=1,
        ) == 4
        # Откат после чтения: записанное переживёт его, если коммит был на
        # месте, и исчезнет, если партию держали одной транзакцией.
        conn.rollback()

    with analytics_session(readonly=True) as conn:
        saved = conn.execute(
            "SELECT COUNT(*) FROM fact_comment_read"
        ).fetchone()[0]
    assert saved == 4, "прочитанное откатилось вместе с сеансом"
