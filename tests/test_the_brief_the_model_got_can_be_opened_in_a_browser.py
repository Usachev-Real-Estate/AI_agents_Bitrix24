"""Выписка по клиенту как страница, а не как файл (раздел 9 ТЗ).

Книга собиралась под два читателя — человека с мышкой и программу с
`curl`, — и третий читатель в них не попал: расширение браузера, которое
видит отрендеренную страницу. Ему список по сто строк и ссылка «расшифровка
↗» на каждый звонок означают десятки переходов, а поток
`application/x-ednjson` браузер вообще скачивает файлом вместо показа — то
есть на месте книги расширение увидело бы пустоту.

`clients.brief` уже собирает про клиента ровно то, что нужно: карточки,
лента, разговоры и честный список того, чего в выписке нет. Не хватало
только вывода наружу в виде, который читают глазами.

Отсюда всё, что проверяется ниже: адрес не должен быть съеден ключом
клиента, тип ответа должен показываться, а не скачиваться, область
видимости обязана спрашиваться ДО чтения разговоров, и телефон не должен
попадать наружу вместе с ними.
"""

import json
import re

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import store
from app import create_app
from clients import review as review_mod
from clients.brief import CARD_FIELDS, CLIENT_FIELDS, _CARD_LABELS, _CLIENT_LABELS
from clients.schema import clients_session, init_clients_db
from config import get_settings

BASE = "/dashboard"
PASSWORD = "correct-horse-battery"
DEPT_A, DEPT_B = 44, 50
MINE, THEIRS = "p:+79001112233", "c:88"
PHONE = "+79001112233"
TALK = "Брокер: здравствуйте.\nКлиент: я подумал и отказываюсь."


@pytest.fixture
def app(tmp_path, analytics_db, monkeypatch):
    """Дашборд с клиентом в отделе А, его сделкой, лентой и одним разговором."""
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    book = tmp_path / "clients.db"
    monkeypatch.setenv("CLIENTS_DB_PATH", str(book))
    get_settings.cache_clear()

    init_clients_db(book)
    with clients_session(book) as conn:
        conn.executemany(
            "INSERT INTO clients(client_key, department_id, name, phone_raw,"
            " triage_state, triage_reason, silence_days, calls_total,"
            " last_event_at, updated_at) VALUES (?, ?, ?, ?, 'cooling',"
            " '6: тишина больше 7 дней', 9, 1,"
            " '2026-09-01T00:00:00+00:00', '')",
            [(MINE, DEPT_A, "Свой Клиент", PHONE),
             (THEIRS, DEPT_B, "Чужой Клиент", "+79997776655")],
        )
        conn.execute(
            "INSERT INTO client_links(client_key, entity_type, entity_id,"
            " stage_id, stage_name, title, date_create, closed)"
            " VALUES (?, 'deal', 7, 'C1:NEW', 'Поиск клиента', 'Квартира', "
            "'2026-05-01T09:00:00+00:00', 0)",
            (MINE,),
        )
        conn.executemany(
            "INSERT INTO client_events(client_key, at, kind, source_id,"
            " author_is_assignee, is_system, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(MINE, "2026-08-20T10:00:00+00:00", "call", "555", 1, 0,
              json.dumps({"direction": 2,
                          "start_time": "2026-08-20T10:00:00+00:00",
                          "end_time": "2026-08-20T10:05:00+00:00"})),
             (MINE, "2026-08-21T10:00:00+00:00", "comment", "c1", 0, 0,
              json.dumps({"body": "Записал\nв две строки"}))],
        )
    # Кэш расшифровок подменяется: тест про вывод выписки, а не про тексты,
    # и открывать настоящую data/violations.db он не должен.
    monkeypatch.setattr(review_mod, "texts_of", lambda ids: {555: TALK})

    application = create_app()
    store.create_user("boss", PASSWORD, "Директор", role="admin")
    store.create_user("rop_b", PASSWORD, "РОП Б", role="rop", department_ids=[DEPT_B])
    yield application
    get_settings.cache_clear()


def _login(app, username):
    session = TestClient(app, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303, response.text[:200]
    return session


def test_the_brief_opens_at_its_own_address_and_is_not_eaten_by_the_client_key(app):
    """Маршрут не перекрыт ручкой клиента.

    `{client_key:path}` забирает и слэши, поэтому порядок объявления решает
    всё: объявленная ниже, выписка досталась бы ручке клиента — та пошла бы
    искать клиента с ключом `p:+7…/brief` и ответила 404. Проверяется
    именно это, а не наличие кода: 404 здесь читался бы как опечатка в
    ключе, и починку искали бы не в порядке маршрутов.
    """
    response = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief")

    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/plain")
    assert "КЛИЕНТ" in response.text


def test_the_brief_shows_the_talk_and_the_card_and_the_feed(app):
    """В выписке есть всё, по чему делают вывод, — и разговор целиком."""
    text = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief").text

    assert "Свой Клиент" in text
    assert "Поиск клиента" in text          # карточка
    assert "направление=исходящий" in text   # лента
    assert "секунд=300" in text               # считается по меткам, не по полю
    assert TALK in text                      # разговор дословно


def test_a_comment_of_two_lines_stays_one_event_in_the_feed(app):
    """Перевод строки внутри записи не разрывает ленту.

    Читатель видит текст, и границы событий у него только эти: склеенный
    перенос превратил бы одну запись в две, а вторую — в событие без даты.
    """
    text = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief").text
    feed = [line for line in text.splitlines() if "kind=comment" in line
            or "что=comment" in line]

    assert len(feed) == 1, feed
    assert "Записал в две строки" in feed[0]


def test_the_brief_says_what_it_did_not_show(app):
    """Раздел про подрезку есть всегда, даже когда показано всё.

    Иначе его отсутствие читалось бы как «ничего не скрыто» ровно так же,
    как и на клиенте с сорока звонками, где скрыто многое.
    """
    text = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief").text

    assert "событий скрыто: 0" in text
    assert "разговоров не показано: 0" in text
    assert "есть на чём отвечать: да" in text


def test_the_brief_does_not_carry_the_phone_number(app):
    """Телефона в выписке нет, хотя в книге он есть.

    Выписка уходит наружу — в чужой сервис или в браузерное расширение, — и
    номер там не нужен, чтобы сказать, что с человеком происходит.
    """
    text = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief").text

    assert PHONE not in text
    # Ключ клиента телефонный, и в адресе он есть — в теле его быть не
    # должно тоже, иначе запрет обходится копированием ключа.
    assert MINE not in text


def test_a_rop_cannot_read_a_brief_from_another_department(app):
    """Область видимости спрашивается ДО чтения разговоров.

    `make_brief` читает таблицы напрямую, области видимости на них нет
    вовсе: перепутанный порядок отдал бы РОПу чужого клиента целиком, с
    расшифровками. И ответ на «чужой» обязан совпадать с ответом на
    «такого нет» — иначе по разнице кодов перебираются ключи книги.
    """
    rop = _login(app, "rop_b")

    assert rop.get(f"{BASE}/api/clients/{MINE}/brief").status_code == 404
    assert rop.get(f"{BASE}/api/clients/p:+70000000000/brief").status_code == 404
    assert rop.get(f"{BASE}/api/clients/{THEIRS}/brief").status_code == 200


def test_the_list_can_be_asked_for_a_type_the_browser_shows(app):
    """`format=text` меняет тип ответа и ничего больше.

    `application/x-ndjson` браузер скачивает файлом вместо показа. Если бы
    параметр менял заодно и содержимое, у книги оказалось бы два разных
    ответа на один вопрос — и расхождение между ними никто не заметил бы.
    """
    boss = _login(app, "boss")
    plain = boss.get(f"{BASE}/api/clients?format=text")
    stream = boss.get(f"{BASE}/api/clients")

    assert plain.headers["content-type"].startswith("text/plain")
    assert stream.headers["content-type"].startswith("application/x-ndjson")
    assert plain.text == stream.text
    assert MINE in plain.text


def test_an_unknown_format_keeps_the_type_for_programs(app):
    """Чужое значение параметра не переключает тип.

    Переключение на «всё, кроме пустого» означало бы, что опечатка в
    строке запроса меняет тип ответа программе, которая про параметр не
    знала.
    """
    response = _login(app, "boss").get(f"{BASE}/api/clients?format=csv")

    assert response.headers["content-type"].startswith("application/x-ndjson")


def test_a_flag_is_shown_as_a_word_and_an_unmeasured_flag_as_a_dash(app):
    """`closed=0` словом, а неизмеренный признак — прочерком.

    Ноль в такой колонке читатель переводит сам, и на этом переводе
    разбирающая модель уже ошиблась: рабочую стадию «Закрытая продажа (На
    сайт)» она прочла как завершённую сделку. А «шаг просрочен: нет» на
    клиенте, которому шаг вообще не назначен, было бы просто неправдой.
    """
    text = _login(app, "boss").get(f"{BASE}/api/clients/{MINE}/brief").text

    assert "закрыта: нет" in text
    assert "агент: нет" in text
    assert "шаг просрочен: —" in text
    assert "ответственный=да" in text


def test_the_client_page_links_to_the_brief_and_the_link_opens(app):
    """Со страницы клиента на выписку ведёт ссылка, и она работает.

    Проверяется не наличие тега, а переход по нему: ключ клиента
    телефонный, с плюсом и двоеточием, и опечатка в шаблоне дала бы ссылку,
    которая тихо ведёт в 404. Заметить это можно было бы только руками.
    """
    boss = _login(app, "boss")
    page = boss.get(f"{BASE}/clients/{MINE}")
    links = re.findall(r'href="([^"]*/brief)"', page.text)

    assert links == [f"{BASE}/api/clients/{MINE}/brief"], page.text[:400]
    assert boss.get(links[0]).status_code == 200


@pytest.mark.parametrize("labels, fields", [
    (_CLIENT_LABELS, CLIENT_FIELDS),
    (_CARD_LABELS, CARD_FIELDS),
])
def test_every_field_the_model_sees_has_a_word_in_the_text_form(labels, fields):
    """Каждое поле выписки подписано по-русски.

    Два списка полей расходятся молча: добавленная колонка осталась бы в
    JSON и исчезла из текста, а заметить это можно было бы только сравнив
    две формы одной выписки глазами.
    """
    labelled = {name for name, _ in labels}

    assert labelled == set(fields), labelled ^ set(fields)
