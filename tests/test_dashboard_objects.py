"""Раздел «Объекты» показывает витрину Афины и не врёт, когда её нет.

Это единственная страница дашборда, данные которой приходят не из витрины, а
по HTTP из другой системы. Отсюда и то, что здесь проверяется: раздела нет,
пока связь не настроена; отказ Афины не роняет страницу; устаревшая причина
снятия не выдаётся за текущую; и раздел не открывается тем, для кого его
нельзя сузить до своего отдела.
"""

import re
import socket
import ssl

import pytest
import web  # noqa: F401  — кладёт src/web и src/analytics на sys.path
from fastapi.testclient import TestClient

import afina
import objects
import store
from app import create_app
from config import get_settings

BASE = "/dashboard"
LOGIN = "chief"
LOGIN_ROP = "rop"
PASSWORD = "correct-horse-battery"
PERIOD = "?start=2026-08-01&end=2026-08-31"

SUMMARY = {"total": 128, "in_ad": 41, "is_published": 33, "removed_from_ad": 17}

# Подпись плитки и её число заданы парами руками. Взять ожидание из
# SUMMARY_TILES значило бы проверять код им же самим: перепутанные местами
# ключи прошли бы такую проверку.
EXPECTED_TILES = (
    ("Всего объектов", "128"),
    ("В рекламе", "41"),
    ("На сайте", "33"),
)

# Ответ Афины в том виде, в каком его отдаёт /listings: даты наивные, без
# смещения; ad_date_* — строки «дд.мм.гггг»; копия не схлопнута с оригиналом.
LISTING = {
    "id": 501,
    "status": "В рекламе",
    "in_ad": True,
    "is_published": True,
    "is_sold": False,
    "closed_sale": False,
    "is_copy": False,
    "original_flat_id": None,
    "removal_reason": None,
    "removal_reason_category": None,
    "removal_comment": None,
    "ad_date_from": "01.08.2026",
    "ad_date_to": "31.08.2026",
    "created_at": "2026-07-01T10:00:00",
    "published_to_ads_at": "2026-08-01T09:00:00",
    "published_to_site_at": "2026-08-02T12:05:00",
    "last_renewed_at": None,
    "removed_from_ad_at": None,
    "title": "Двушка у парка",
    "address": "Кубанская Набережная, 39",
    "complex_name": "ЖК Резиденция",
    "district": "Центральный",
    "price": 9500000,
    "area": 62.4,
    "assignee_name": "Тестов Иван",
    "department_name": "Отдел Трофимовой",
    "ad_account_name": "Основной аккаунт",
}

REMOVED = dict(
    LISTING,
    id=502,
    status="Снят с рекламы",
    in_ad=False,
    is_published=False,
    removal_reason="Другое: собственник уехал",
    removal_reason_category="Другое",
    removal_comment="собственник уехал",
    removed_from_ad_at="2026-08-20T14:30:00",
    title="Однушка на Красной",
)

REMOVAL_EVENT = {
    "flat_id": 502,
    "removed_at": "2026-08-20T14:30:00",
    "removal_reason": "Другое: собственник уехал",
    "removal_reason_category": "Другое",
    "removal_comment": "собственник уехал",
    # Статус на сейчас отличается от статуса на момент снятия — ради этого
    # вкладка и нужна. Значение выбрано так, чтобы не встречаться на странице
    # больше нигде: иначе проверка прошла бы на подписи фильтра.
    "current_status": "Отложенный спрос",
    "title": "Однушка на Красной",
    "address": "Красная, 12",
    "complex_name": "ЖК Центральный",
    "assignee_name": "Тестов Иван",
    "department_name": "Отдел Трофимовой",
}


def _visible(body):
    """Разметка без значений атрибутов — только то, что человек читает.

    Иначе проверка «причина показана» проходит на подсказке title даже
    тогда, когда сама ячейка пуста.
    """
    return re.sub(r'\s+[\w-]+="[^"]*"', "", body)


def _page(items):
    rows = list(items)
    return {"items": rows, "total": len(rows), "page": 1, "size": 50,
            "total_pages": 1}


class FakeAfina:
    """Афина, которая отвечает из памяти и запоминает, о чём её спросили."""

    def __init__(self, listings=(), removals=(), summary=None, fail=None):
        self._listings = list(listings)
        self._removals = list(removals)
        self._summary = SUMMARY if summary is None else summary
        self._fail = fail
        self.calls = []

    def summary(self, **kwargs):
        self.calls.append(("summary", kwargs))
        self._maybe_fail()
        return self._summary

    def listings(self, **kwargs):
        self.calls.append(("listings", kwargs))
        self._maybe_fail()
        return _page(afina._with_utc_dates(dict(item)) for item in self._listings)

    def fetch_all(self, *, cap=5000, **filters):
        """Вся выборка сразу — так же, как её собирает настоящий клиент."""
        self.calls.append(("fetch_all", filters))
        self._maybe_fail()
        rows = [afina._with_utc_dates(dict(x))
                for x in self._listings if self._matches(x, filters)]
        return rows[:cap]

    def count(self, **filters):
        """Считает по тем же правилам, что и Афина, — иначе плитки не проверить."""
        self.calls.append(("count", filters))
        self._maybe_fail()
        return len([x for x in self._listings if self._matches(x, filters)])

    @staticmethod
    def _matches(item, filters):
        status = (item.get("status") or "").strip()
        if filters.get("in_ad") and status != "В рекламе":
            return False
        if filters.get("removed_from_ad") and status != "Снят с рекламы":
            return False
        if filters.get("is_published") and not item.get("is_published"):
            return False
        needle = (filters.get("search") or "").strip().lower()
        if needle:
            haystack = " ".join(
                str(item.get(field) or "")
                for field in ("address", "complex_name", "title", "district")
            ).lower()
            if needle not in haystack:
                return False
        return True

    def counts_of(self, **filters):
        """Все запросы счётчика, совпавшие по этим параметрам."""
        return [kw for name, kw in self.calls
                if name == "count" and all(kw.get(k) == v for k, v in filters.items())]

    def listing(self, flat_id):
        self.calls.append(("listing", {"flat_id": flat_id}))
        self._maybe_fail()
        for item in self._listings:
            if item["id"] == flat_id:
                return afina._with_utc_dates(dict(item))
        return None

    def removals(self, **kwargs):
        self.calls.append(("removals", kwargs))
        self._maybe_fail()
        return _page(afina._with_utc_dates(dict(item)) for item in self._removals)

    def kwargs_of(self, method):
        return next(kwargs for name, kwargs in self.calls if name == method)

    def _maybe_fail(self):
        if self._fail is not None:
            raise self._fail


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    """Снимок кэшируется в модуле, а не в приложении.

    Без сброса второй тест увидел бы объекты первого и прошёл бы по чужим
    данным — или упал бы, не объяснив почему.
    """
    objects.reset_cache()
    yield
    objects.reset_cache()


@pytest.fixture
def afina_api(monkeypatch):
    """Афина по умолчанию: один объект в рекламе, один снятый, одно снятие."""
    fake = FakeAfina(listings=[LISTING, REMOVED], removals=[REMOVAL_EVENT])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    return fake


@pytest.fixture
def app_factory(analytics_db, monkeypatch):
    """Приложение с настраиваемым ключом Афины.

    Ключ задаётся до create_app и до сброса кеша настроек: раздел решает,
    показываться ли ему, по настройкам, а не по запросу.
    """
    def build(api_key="afina-test-key", page_size=None):
        monkeypatch.setenv("DASHBOARD_SECRET_KEY", "o" * 48)
        monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
        monkeypatch.setenv("AFINA_API_KEY", api_key)
        monkeypatch.setenv("AFINA_API_BASE_URL", "https://afina.example")
        if page_size is not None:
            monkeypatch.setenv("AFINA_API_PAGE_SIZE", str(page_size))
        get_settings.cache_clear()
        application = create_app()
        store.create_user(LOGIN, PASSWORD, "Руководитель", role="admin")
        store.create_user(LOGIN_ROP, PASSWORD, "РОП", role="rop",
                          department_ids=[44])
        return application

    return build


def _login(application, username=LOGIN):
    session = TestClient(application, follow_redirects=False)
    session.get(f"{BASE}/login")
    response = session.post(f"{BASE}/login", data={
        "username": username, "password": PASSWORD,
        "csrf_token": session.cookies.get("dash_csrf"), "next": "",
    })
    assert response.status_code == 303
    return session


@pytest.fixture
def client(app_factory):
    return _login(app_factory())


# --- раздел появляется только когда есть чем его наполнить ---


def test_section_is_hidden_until_afina_is_configured(app_factory):
    """Без ключа пункта меню нет.

    Пункт, ведущий на плашку «не настроено», хуже отсутствующего: по нему
    не понять, поломка это или так и задумано.
    """
    session = _login(app_factory(api_key=""))
    assert "Объекты" not in session.get(f"{BASE}/").text


def test_unconfigured_page_explains_itself_instead_of_calling_afina(
    app_factory, monkeypatch,
):
    """Открытый напрямую адрес объясняет, чего не хватает, и никуда не ходит.

    Проверяется именно «не ходит»: клиент даже не создаётся. Без этого
    регресс в проверке настройки означал бы запрос в сеть на каждый показ
    страницы, а тест бы молчал.
    """
    built = []
    monkeypatch.setattr(objects, "client_for", lambda settings: built.append(settings))
    session = _login(app_factory(api_key=""))
    response = session.get(f"{BASE}/objects")
    assert response.status_code == 200
    assert "Связь с Афиной не настроена" in response.text
    assert built == []


def test_configured_section_appears_in_navigation(client, afina_api):
    assert "Объекты" in client.get(f"{BASE}/").text


# --- цифры и плитки ---

# Третий объект — другой отдел и другой брокер: без него разбивка состоит из
# одной группы и не проверяет ни группировку, ни сортировку.
OTHER = dict(
    LISTING,
    id=503,
    title="Трёшка у моря",
    address="Морская, 7",
    assignee_name="Петров Пётр",
    department_name="Отдел Ким",
    published_to_ads_at="2026-08-15T10:00:00",
    published_to_site_at="2026-08-15T10:00:00",
)

# Снят и с рекламы, и с сайта: единственный способ проверить плитку «Сняты с
# сайта», которую Афина отдаёт состоянием site_sync_status.
OFF_SITE = dict(
    REMOVED,
    id=504,
    title="Студия на Ленина",
    site_sync_status="unpublished",
    published_to_ads_at="2026-01-20T10:00:00",
    assignee_name="Петров Пётр",
    department_name="Отдел Ким",
)


@pytest.fixture
def wide_afina(monkeypatch):
    """Четыре объекта, два отдела, два брокера — минимум для разбивки."""
    fake = FakeAfina(listings=[LISTING, REMOVED, OTHER, OFF_SITE],
                     removals=[REMOVAL_EVENT])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    return fake


def test_counters_come_from_afina(client, afina_api):
    """Без фильтра плитки — по всей базе, парами «подпись — число».

    Проверяется пара, а не набор чисел: перепутанные ключи дают четыре
    правильных числа на странице и четыре неправильные подписи.
    """
    body = client.get(f"{BASE}/objects").text
    for label, value in EXPECTED_TILES:
        marker = f'<div class="tile-label">{label}</div>'
        assert marker in body, label
        assert value in body.split(marker, 1)[1][:200], label


def test_the_whole_base_view_does_not_download_the_base(client, afina_api):
    """«Все» считает сама Афина: 38 тысяч карточек постранично не собрать."""
    client.get(f"{BASE}/objects")
    assert [name for name, _ in afina_api.calls if name == "fetch_all"] == []


def test_tiles_describe_the_scope_not_the_chip(client, wide_afina):
    """Чип не сужает плитки — иначе половина из них показывала бы ноль.

    Под фильтром «в рекламе» плитка «Сняты с рекламы» обязана показать
    снятые объекты отдела, а не ноль: чип выбирает метрику и таблицу, а не
    выборку для счётчиков.
    """
    body = client.get(f"{BASE}/objects?filter=in_ad{PERIOD.replace('?', '&')}").text
    values = _tile_values(body)
    labels = _tile_labels(body)
    assert labels[:3] == ["В рекламе", "На сайте", "Сняты с рекламы"]
    # Два в рекламе (501, 503), два снятых (502, 504).
    assert values[0] == "2"
    assert values[2] == "2"


def test_period_tile_counts_only_events_inside_the_window(client, wide_afina):
    """Четвёртая плитка — события за период, а не состояние.

    В окне 01–31 августа лежат обе публикации в рекламу; за пределами окна
    плитка обязана обнулиться, иначе период ни на что не влияет.
    """
    inside = client.get(f"{BASE}/objects?filter=in_ad{PERIOD.replace('?', '&')}").text
    assert _tile_labels(inside)[3] == "Выставлено в рекламу за период"
    # Три публикации в августе; четвёртая — в январе и сюда не попадает.
    assert _tile_values(inside)[3] == "3"

    january = client.get(
        f"{BASE}/objects?filter=in_ad&start=2026-01-01&end=2026-01-31").text
    assert _tile_values(january)[3] == "1"


def test_site_filter_shows_site_tiles(client, wide_afina):
    """У фильтра «на сайте» свой набор: сайт, снятые с сайта, выставлено."""
    body = client.get(f"{BASE}/objects?filter=published{PERIOD.replace('?', '&')}").text
    assert _tile_labels(body)[:2] == ["На сайте", "Сняты с сайта"]
    assert _tile_labels(body)[3] == "Выставлено на сайт за период"
    # Снят с сайта ровно один объект — тот, у кого site_sync_status=unpublished.
    assert _tile_values(body)[1] == "1"


def test_site_removed_tile_admits_it_has_no_period(client, wide_afina):
    """Даты снятия с сайта в Афине нет, и плитка обязана про это сказать.

    Иначе её прочитают как «снято за выбранный период» и посчитают неверно.
    """
    body = client.get(f"{BASE}/objects?filter=published").text
    assert "даты снятия Афина не хранит" in body


def test_the_whole_base_no_longer_shows_removed_from_ad(client, afina_api):
    """Плитку «Сняты с рекламы» из общего вида убрали."""
    labels = _tile_labels(client.get(f"{BASE}/objects").text)
    assert labels == ["Всего объектов", "В рекламе", "На сайте"]


# --- что считается снятием ---


@pytest.mark.parametrize("reason, category, comment, counted", [
    ("Другое: собственник уехал", "Другое", "собственник уехал", True),
    ("Другое", "Другое", None, False),
    ("Другое:", "Другое", None, False),
    ("Продано нами", "Продано нами", None, True),
    ("По истечению срока публикации", "По истечению срока публикации", None, True),
    (None, None, None, False),
])
def test_only_an_explained_removal_counts(reason, category, comment, counted):
    """«Другое» без пояснения — не причина, а несделанная работа.

    Требовать комментарий от всех причин нельзя: у словарных его нет по
    устройству Афины, и тогда не засчитывалось бы вообще ничего.
    """
    item = dict(REMOVED, removal_reason=reason, removal_reason_category=category,
                removal_comment=comment)
    assert objects.counts_as_removal(item) is counted


def test_an_object_in_ad_is_never_a_removal():
    """Причина у вернувшегося в рекламу остаётся в поле, снятием он не стал."""
    assert objects.counts_as_removal(dict(LISTING, removal_reason="Продано нами",
                                          removal_reason_category="Продано нами")) is False


def test_unexplained_removals_are_not_counted_on_the_page(app_factory, monkeypatch):
    """Снятое с «Другое» без пояснения не попадает в счётчик снятий."""
    vague = dict(REMOVED, id=505, removal_reason="Другое",
                 removal_reason_category="Другое", removal_comment=None)
    fake = FakeAfina(listings=[LISTING, REMOVED, vague])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    body = _login(app_factory()).get(f"{BASE}/objects?filter=in_ad").text
    # Снятых объектов два, но засчитано одно — у второго причина пустая.
    assert _tile_values(body)[2] == "1"


# --- прирост к прошлому периоду ---


def _published_at(flat_id, moment):
    """Объект в рекламе с заданной датой публикации и своим брокером."""
    return dict(LISTING, id=flat_id, published_to_ads_at=moment,
                assignee_name=f"Брокер {flat_id}")


@pytest.fixture
def dated_afina(monkeypatch):
    """Две публикации в окне «16–31 августа» и одна в предыдущем такой же длины.

    Окно считается по московскому календарю, поэтому даты взяты с запасом от
    границ: проверяется рост, а не арифметика полуночи.
    """
    fake = FakeAfina(listings=[
        _published_at(601, "2026-08-20T10:00:00"),
        _published_at(602, "2026-08-21T10:00:00"),
        _published_at(603, "2026-08-05T10:00:00"),
    ])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    return fake


def test_period_tile_shows_growth_against_the_previous_window(client, dated_afina):
    """Процент считается к периоду той же длины, как в остальном дашборде.

    В окне 16–31 августа две публикации, в предыдущем такой же длины — одна:
    рост вдвое.
    """
    body = client.get(
        f"{BASE}/objects?filter=in_ad&start=2026-08-16&end=2026-08-31").text
    assert _tile_values(body)[3] == "2"
    # При наличии процента макет плитки печатает «к прошлому периоду» вместо
    # подсказки — так же, как на остальных страницах дашборда.
    assert "+100%" in _tile_hints(body)[3]
    assert "к прошлому периоду" in _tile_hints(body)[3]


def test_growth_from_zero_is_words_not_a_number(client, dated_afina):
    """Рост с нуля — не «+∞» и не «+100%», а «в прошлом периоде не было».

    Окно 01–15 августа: одна публикация, а в предыдущем (17–31 июля) ни одной.
    """
    body = client.get(
        f"{BASE}/objects?filter=in_ad&start=2026-08-01&end=2026-08-15").text
    assert _tile_values(body)[3] == "1"
    assert "За прошлый период — ни одного" in body
    assert "%" not in _tile_hints(body)[3]


# --- отделы ---


def test_department_options_come_from_the_data(client, wide_afina):
    """Справочник отделов Афина по API не отдаёт — собираем из снимка."""
    body = client.get(f"{BASE}/objects?filter=in_ad").text
    assert "Отдел Трофимовой" in body
    assert "Отдел Ким" in body


def test_department_narrows_tiles_and_table(client, wide_afina):
    """Выбранный отдел сужает и плитки, и разбивку.

    В отделе Ким один объект в рекламе из двух и один снятый из двух.
    """
    body = client.get(
        f"{BASE}/objects?filter=in_ad&department=Отдел+Ким{PERIOD.replace('?', '&')}").text
    assert _tile_values(body)[0] == "1"
    assert "Петров Пётр" in body
    assert "Тестов Иван" not in body


# --- таблица по отделам и брокерам ---


def test_breakdown_replaces_the_object_list(client, wide_afina):
    """Основная таблица теперь про отделы и брокеров, а не про объекты."""
    body = client.get(f"{BASE}/objects?filter=in_ad").text
    assert "Отделы и брокеры" in body
    # Заголовков старого списка объектов на странице быть не должно.
    assert "Период рекламы" not in body
    assert "Рекламный аккаунт" not in body


@pytest.mark.parametrize("chip, expected", [
    ("in_ad", ["В рекламе", "На сайте", "Выставлено в рекламу за период"]),
    ("published", ["На сайте", "Сняты с сайта", "Выставлено на сайт за период"]),
    ("removed", ["Сняты с рекламы", "На сайте", "Снято с рекламы за период"]),
])
def test_breakdown_columns_follow_the_chip(client, wide_afina, chip, expected):
    """Колонки таблицы — про выбранный фильтр, а не все срезы сразу.

    Под «в рекламе» колонка «сняты с сайта» ни о чём не говорит, а пять
    колонок подряд читаются хуже трёх.
    """
    body = client.get(f"{BASE}/objects?filter={chip}").text
    head = body.split("Отдел и брокер", 1)[1].split("</thead>", 1)[0]
    assert re.findall(r'<th class="num nowrap">([^<]+)</th>', head) == expected


def test_breakdown_counts_every_broker(client, wide_afina):
    """У каждого брокера своя строка со своими числами."""
    body = _visible(client.get(f"{BASE}/objects?filter=in_ad").text)
    assert "Тестов Иван" in body
    assert "Петров Пётр" in body
    assert "Итого" in body


def test_broker_row_opens_his_objects(client, wide_afina):
    """Строка брокера без раскрытия — тупик: число есть, объектов не видно."""
    body = client.get(f"{BASE}/objects?filter=in_ad&broker=Петров+Пётр").text
    assert "Трёшка у моря" in body
    # Чужие объекты в раскрытии не показываются.
    assert "Двушка у парка" not in body


def test_broker_drilldown_respects_the_chip(client, wide_afina):
    """Раскрытие показывает те объекты, по которым считалась колонка чипа."""
    body = client.get(f"{BASE}/objects?filter=removed&broker=Петров+Пётр").text
    assert "Студия на Ленина" in body
    assert "Трёшка у моря" not in body


# --- снимок ---


def test_snapshot_is_taken_once_and_reused(client, wide_afina):
    """Снимок живёт минуту: переключение чипов не должно бить по Афине.

    Иначе каждый клик — десяток запросов, и раздел становится дороже всего
    остального дашборда вместе взятого.
    """
    client.get(f"{BASE}/objects?filter=in_ad")
    first = len([1 for name, _ in wide_afina.calls if name == "fetch_all"])
    client.get(f"{BASE}/objects?filter=published")
    client.get(f"{BASE}/objects?filter=removed")
    assert first == 3, "снимок собирается из трёх выборок"
    assert len([1 for name, _ in wide_afina.calls if name == "fetch_all"]) == first


def test_snapshot_merges_selections_instead_of_summing_them(client, wide_afina):
    """Объект бывает и в рекламе, и на сайте — считать его дважды нельзя."""
    body = client.get(f"{BASE}/objects?filter=in_ad").text
    # 501 и 503 в рекламе и одновременно на сайте; без слияния по id
    # «В рекламе» показало бы четыре вместо двух.
    assert _tile_values(body)[0] == "2"


def test_stale_reason_is_not_shown_for_an_object_back_in_ad(app_factory, monkeypatch):
    """У вернувшегося в рекламу объекта старая причина не показывается.

    Афина не чистит поле причины при возврате в рекламу. Напечатать рядом со
    статусом «В рекламе» прошлое «Продано другими» значит соврать.
    """
    stale = dict(LISTING, removal_reason="Продано другими",
                 removal_reason_category="Продано другими")
    fake = FakeAfina(listings=[stale])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    body = _login(app_factory()).get(f"{BASE}/objects?filter=in_ad&broker=Тестов+Иван").text
    assert "Продано другими" not in body


# --- период, карточка, снятия ---


def test_removals_view_asks_for_the_chosen_period(client, afina_api):
    """История снятий берёт границы из фильтра периода.

    Афина ждёт включающие календарные даты, а не since/until витрины, у
    которой правая граница исключающая.
    """
    client.get(f"{BASE}/objects?view=removals{PERIOD.replace('?', '&')}")
    kwargs = afina_api.kwargs_of("removals")
    assert kwargs["since"] == "2026-08-01"
    assert kwargs["until"] == "2026-08-31"


def test_removals_view_shows_the_event_reason(client, afina_api):
    """Причина на момент события и статус на сейчас стоят рядом."""
    body = _visible(client.get(f"{BASE}/objects?view=removals").text)
    assert "собственник уехал" in body
    assert "Отложенный спрос" in body


def test_page_size_never_exceeds_afinas_ceiling():
    """Просить больше сотни бессмысленно: Афина ответит 422."""
    assert afina.MAX_PAGE_SIZE == 100
    assert afina._clamp_size(500) == 100
    assert afina._clamp_size(0) == 1


def test_single_object_card_opens_by_id(client, afina_api):
    body = client.get(f"{BASE}/objects?id=501").text
    assert "Двушка у парка" in body
    assert afina_api.kwargs_of("listing")["flat_id"] == 501


def test_card_shows_the_ad_period_and_the_reason_it_was_removed(client, afina_api):
    """Карточку открывают ради этих двух строк.

    Их легко потерять: они не приходят от Афины готовыми, а собираются из
    полей — период из двух дат, причина только у снятых.
    """
    body = client.get(f"{BASE}/objects?id=502").text
    assert "01.08.2026 — 31.08.2026" in body
    assert "Другое: собственник уехал" in body


def test_missing_object_says_so(app_factory, monkeypatch):
    fake = FakeAfina(listings=[])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    body = _login(app_factory()).get(f"{BASE}/objects?id=999").text
    assert "Объект 999 в Афине не найден" in body


def test_the_card_keeps_the_whole_base_tiles(client, afina_api):
    """На карточке объекта фильтр списка ни на что не влияет."""
    body = client.get(f"{BASE}/objects?id=501&filter=removed").text
    assert _tile_labels(body)[0] == "Всего объектов"


def test_removals_view_keeps_the_whole_base_tiles(client, afina_api):
    """История снятий — события, а плитки описывают состояние на сейчас."""
    body = client.get(f"{BASE}/objects?view=removals").text
    assert _tile_labels(body)[0] == "Всего объектов"


def _tile_values(body):
    """Числа плиток в порядке слева направо."""
    return re.findall(r'<div class="tile-value[^"]*">([^<]+)</div>', body)


def _tile_labels(body):
    return re.findall(r'<div class="tile-label">([^<]+)</div>', body)


def _tile_hints(body):
    """Подписи под числами: там живут и проценты, и пояснения."""
    return re.findall(r'<div class="tile-delta">(.*?)</div>', body, re.S)


# --- отказ источника ---


def test_afina_failure_keeps_the_page_alive(app_factory, monkeypatch):
    """Молчащая Афина — плашка, а не пятисотая.

    Человеку нужнее понять, что сломалось, чем увидеть страницу ошибки без
    навигации.
    """
    fake = FakeAfina(fail=afina.AfinaError("Афина не ответила за 10 с"))
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    response = _login(app_factory()).get(f"{BASE}/objects")
    assert response.status_code == 200
    assert "Афина не ответила за 10 с" in response.text


def test_api_reports_a_dead_source_as_bad_gateway(app_factory, monkeypatch):
    fake = FakeAfina(fail=afina.AfinaError("Афина недоступна"))
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    response = _login(app_factory()).get(f"{BASE}/api/objects")
    assert response.status_code == 502
    assert response.json()["error"] == "Афина недоступна"


def test_a_missing_object_is_not_reported_as_an_outage(client, afina_api):
    """Опечатка в id — это 404, а не авария источника.

    502 по чужой опечатке поднимает мониторинг и заставляет искать поломку
    там, где Афина жива и ответила по существу.
    """
    response = client.get(f"{BASE}/api/objects?id=999")
    assert response.status_code == 404
    # Карточка не сужает выдачу, поэтому плитки остаются по всей базе.
    assert response.json()["scoped"] is False
    assert [tile["value"] for tile in response.json()["tiles"]] == [
        SUMMARY["total"], SUMMARY["in_ad"], SUMMARY["is_published"],
    ]


def test_a_missing_endpoint_is_not_reported_as_a_missing_object():
    """404 на /summary значит, что бэкенд Афины не пересобран.

    Сказать про это «объект не найден» — отправить искать не там: объекта
    никто и не спрашивал.
    """
    import httpx

    generic = afina._error_for("/summary", httpx.Response(404, text="{}"))
    assert not isinstance(generic, afina.AfinaNotFound)
    assert "витрины Афины нет" in str(generic)

    single = afina._error_for(
        "/listings/9", httpx.Response(404, text="{}"), allow_not_found=True,
    )
    assert isinstance(single, afina.AfinaNotFound)


def test_a_truncated_summary_shows_a_dash_instead_of_crashing(app_factory, monkeypatch):
    """Афина недосчиталась ключа — плитка пустая, а не пятисотая.

    Шаблон обращается к ключам сводки по одному, и отсутствующий ключ в
    Jinja — не None, а Undefined: он рушит отрисовку всей страницы.
    """
    fake = FakeAfina(listings=[LISTING], summary={"total": 5, "in_ad": 2})
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    response = _login(app_factory()).get(f"{BASE}/objects")
    assert response.status_code == 200
    # Недостающие ключи превращаются в прочерк, а не в пятисотую.
    assert _tile_values(response.text) == ["5", "2", "—"]


def test_period_control_is_hidden_on_a_single_object(client, afina_api):
    """На карточке период ни на что не влияет — значит, его там нет.

    Переключатель, который ничего не меняет, читается как сломанный фильтр.
    """
    card = client.get(f"{BASE}/objects?view=removals&id=501").text
    listing = client.get(f"{BASE}/objects?view=removals").text
    assert "Текущий квартал" in listing
    assert "Текущий квартал" not in card


def test_navigation_does_not_carry_another_sections_filters(client, afina_api):
    """Из «Таблицы» в «Объекты» не должны уезжать её поиск и номер страницы.

    Иначе раздел открывается пустой третьей страницей с поиском, которого
    человек в нём не набирал.
    """
    body = client.get(f"{BASE}/table?q=Иванов&page=3&entity=deal").text
    link = body.split('href="/dashboard/objects?', 1)[1].split('"', 1)[0]
    assert "q=" not in link
    assert "page=" not in link


# --- доступ ---


def test_rop_cannot_open_the_section(app_factory, afina_api):
    """РОПу раздел не открывается, потому что его нечем сузить.

    Отделы Афины — её собственный справочник, сопоставить его с отделами
    Битрикса нечем. Показать РОПу брокеров всей компании значит обойти то,
    ради чего сделана область видимости витрины.
    """
    session = _login(app_factory(), username=LOGIN_ROP)
    assert session.get(f"{BASE}/objects").status_code == 403
    assert session.get(f"{BASE}/api/objects").status_code == 403


def test_rop_does_not_see_the_section_in_navigation(app_factory, afina_api):
    session = _login(app_factory(), username=LOGIN_ROP)
    assert "Объекты" not in session.get(f"{BASE}/").text


def test_anonymous_gets_nothing(app_factory, afina_api):
    session = TestClient(app_factory(), follow_redirects=False)
    assert session.get(f"{BASE}/objects").status_code == 303
    assert session.get(
        f"{BASE}/api/objects", headers={"Accept": "application/json"},
    ).status_code == 401


# --- утечки и разметка ---


def test_the_afina_key_never_reaches_the_page(client, afina_api):
    """Ключ — серверный секрет. В разметке ему делать нечего."""
    for path in ("/objects", "/api/objects"):
        assert "afina-test-key" not in client.get(f"{BASE}{path}").text


def test_hostile_content_from_afina_is_escaped(app_factory, monkeypatch):
    """Афина — тоже внешний источник, её строки пишут люди.

    Адрес и название приходят из карточки, которую заполняет брокер: то же
    правило, что и для заголовков сделок из Битрикса.
    """
    hostile = dict(
        LISTING,
        title="<img src=x onerror=alert(1)>",
        address='" onmouseover="alert(1)',
        assignee_name="</script><script>alert(document.cookie)</script>",
    )
    fake = FakeAfina(listings=[hostile])
    monkeypatch.setattr(objects, "client_for", lambda settings: fake)
    body = _login(app_factory()).get(f"{BASE}/objects").text
    assert "<img src=x onerror=" not in body
    assert "<script>alert(" not in body
    assert 'onmouseover="alert' not in body


@pytest.mark.parametrize("cause, expected", [
    (socket.gaierror("Name or service not known"), "не разрешается в адрес"),
    (ConnectionRefusedError(111, "Connection refused"), "отказала в соединении"),
    (ssl.SSLCertVerificationError("certificate has expired"),
     "Сертификат Афины не принят"),
])
def test_an_unreachable_afina_says_which_link_broke(cause, expected):
    """DNS, отказ соединения и сертификат — три разные поломки.

    httpx складывает их в один ConnectError, и одно «Афина недоступна» на
    всех отправляет администратора гадать: чинить адрес, сеть или TLS.
    """
    import httpx as _httpx

    error = _httpx.ConnectError("boom")
    error.__cause__ = cause
    assert expected in afina._transport_reason(error)


def test_an_unexplained_transport_failure_still_says_something_useful():
    """Причины в цепочке нет — говорим хотя бы, откуда не достучались."""
    import httpx as _httpx

    reason = afina._transport_reason(_httpx.ConnectError("boom"))
    assert "с сервера дашборда" in reason


def test_a_proxy_in_the_way_is_named(afina_over_transport):
    """Прокси в окружении — частая причина, и искать её надо в окружении."""
    import httpx as _httpx

    def handler(request):
        raise _httpx.ProxyError("no route")

    client = afina_over_transport(handler)
    with pytest.raises(afina.AfinaError) as failure:
        client.summary()
    assert "HTTP_PROXY" in str(failure.value)


def test_a_broken_base_url_is_a_notice_not_a_crash():
    """Опечатка в адресе Афины — такая же плашка, как её недоступность.

    httpx.InvalidURL — единственное его исключение вне иерархии HTTPError,
    и без отдельного except оно уходит мимо страницы в обработчик пятисотых.
    """
    client = afina.AfinaClient("http://[::1", "ключ", timeout=1.0)
    with pytest.raises(afina.AfinaError) as failure:
        client.summary()
    assert "AFINA_API_BASE_URL" in str(failure.value)


@pytest.fixture
def afina_over_transport(monkeypatch):
    """Клиент Афины поверх поддельного транспорта httpx — без сети."""
    import httpx as _httpx

    def build(handler):
        original = _httpx.Client

        def stubbed(**kwargs):
            return original(transport=_httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(afina.httpx, "Client", stubbed)
        return afina.AfinaClient("https://afina.example", "afina-test-key", timeout=5.0)

    return build


@pytest.mark.parametrize("status, body, expected", [
    (401, '{"detail": "x"}', "Афина не приняла ключ"),
    (403, '{"detail": "x"}', "Афина не приняла ключ"),
    (503, '{"detail": "x"}', "не настроен PUBLIC_API_KEY"),
    (422, '{"detail": []}', "не приняла параметры"),
    (500, "boom", "ошибкой 500"),
    (200, "не json", "не JSON"),
])
def test_afina_refusals_say_what_to_fix(afina_over_transport, status, body, expected):
    """Тексты отказов — это диагностика в DEPLOY.md, а не украшение.

    Их читает тот, кто разворачивает дашборд: в таблице диагностики они
    выписаны дословно, и молча менять их нельзя.
    """
    import httpx as _httpx

    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-API-Key")
        seen["url"] = str(request.url)
        return _httpx.Response(status, text=body)

    client = afina_over_transport(handler)
    with pytest.raises(afina.AfinaError) as failure:
        client.summary()
    assert expected in str(failure.value)
    # Ключ уходит заголовком, а не в адресе: адрес попадает в логи прокси.
    assert seen["key"] == "afina-test-key"
    assert "afina-test-key" not in seen["url"]
    assert seen["url"].endswith("/api/public/dashboard/summary")


def test_a_missing_object_returns_none_not_an_error(afina_over_transport):
    """404 на карточке — штатный ответ, его клиент гасит сам."""
    import httpx as _httpx

    client = afina_over_transport(
        lambda request: _httpx.Response(404, text='{"detail": "Объект не найден"}'),
    )
    assert client.listing(999) is None


def test_filters_reach_afina_as_query_parameters(afina_over_transport):
    """Булевы фильтры и поиск уходят строкой запроса, а не телом."""
    import httpx as _httpx

    seen = {}

    def handler(request):
        seen["query"] = str(request.url.query, "utf-8")
        return _httpx.Response(200, json={"items": [], "total": 0, "page": 1,
                                          "size": 20, "total_pages": 1})

    client = afina_over_transport(handler)
    client.listings(in_ad=True, is_published=False, search="Кубанская", size=500)
    assert "in_ad=true" in seen["query"]
    assert "is_published=false" in seen["query"]
    # Потолок Афины: больше сотни она отвергнет с 422.
    assert "size=100" in seen["query"]


def test_a_key_with_cyrillic_is_a_notice_not_a_crash():
    """Русская «с» вместо латинской в ключе — плашка, а не пятисотая.

    Заголовок HTTP кодируется в latin-1, и httpx падает на этом ещё до
    запроса — исключением, которого нет в иерархии HTTPError.
    """
    client = afina.AfinaClient("https://afina.example", "ключ", timeout=1.0)
    with pytest.raises(afina.AfinaError) as failure:
        client.summary()
    assert "AFINA_API_KEY" in str(failure.value)


def test_naive_afina_dates_are_read_as_utc():
    """Даты Афины наивные, но это UTC — иначе они уедут на три часа.

    Фильтры шаблонов переводят время в московское через astimezone, а тот
    для наивной даты берёт пояс сервера.
    """
    item = afina._with_utc_dates({"created_at": "2026-07-01T10:00:00"})
    assert item["created_at"] == "2026-07-01T10:00:00+00:00"


def test_ad_dates_stay_strings():
    """ad_date_from/ad_date_to — строки «дд.мм.гггг», их не разбирают."""
    item = afina._with_utc_dates({"ad_date_from": "01.08.2026"})
    assert item["ad_date_from"] == "01.08.2026"


def test_booleans_go_to_afina_as_true_and_false():
    """httpx иначе отправил бы True/False, которых FastAPI не понимает."""
    assert afina._query({"in_ad": True, "is_published": False}) == {
        "in_ad": "true", "is_published": "false",
    }


def test_empty_parameters_are_not_sent():
    """Пустой status у Афины означает «ничего не подходит», а не «без фильтра»."""
    assert afina._query({"search": None, "status": [], "page": 1}) == {"page": 1}
