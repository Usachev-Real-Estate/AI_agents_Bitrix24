"""ETL витрины: загрузка, инкремент и вычисление удалённых."""

import analytics  # noqa: F401  — кладёт src/analytics на sys.path
import etl
import pytest
from schema import analytics_session


class FakeClient:
    """Заглушка Bitrix REST с управляемыми ответами."""

    def __init__(self, deals=None, leads=None, history=None):
        self.deals = deals or []
        self.leads = leads or []
        self.history = history or {}
        self.request_count = 0
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self):
        pass

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "crm.category.list":
            return {"categories": [
                {"id": 0, "name": "Продавцы", "sort": 10},
                {"id": 18, "name": "Покупатели", "sort": 20},
            ]}
        if method == "crm.status.list":
            entity = (params or {}).get("filter", {}).get("ENTITY_ID", "")
            if entity == "DEAL_STAGE_18":
                return [
                    {"STATUS_ID": "C18:NEW", "NAME": "Подбор", "SORT": 10},
                    {"STATUS_ID": "C18:WON", "NAME": "Договор закрыт", "SORT": 90},
                    {"STATUS_ID": "C18:APOLOGY", "NAME": "Сделка проиграна", "SORT": 95},
                ]
            if entity == "DEAL_STAGE":
                return [{"STATUS_ID": "NEW", "NAME": "Назначение встречи", "SORT": 10}]
            if entity == "STATUS":
                return [
                    {"STATUS_ID": "NEW", "NAME": "Не обработан", "SORT": 10},
                    {"STATUS_ID": "CONVERTED", "NAME": "Квалифицирован", "SORT": 20},
                    {"STATUS_ID": "JUNK", "NAME": "Спам", "SORT": 30},
                ]
            if entity == "SOURCE":
                return [{"STATUS_ID": "CALL", "NAME": "Звонок"}]
        if method == "department.get":
            return [{"ID": 44, "NAME": "Отдел Трофимовой"}]
        if method == "profile":
            return {"ID": 1, "NAME": "Тест"}
        return []

    def call_envelope(self, method, params=None):
        return {"result": self.call(method, params)}

    def list_by_id(self, method, params):
        self.calls.append((method, params))
        rows = self.deals if method == "crm.deal.list" else self.leads
        flt = params.get("filter", {})
        for row in rows:
            if ">=DATE_MODIFY" in flt and row.get("DATE_MODIFY", "") < flt[">=DATE_MODIFY"]:
                continue
            if ">=DATE_CREATE" in flt and row.get("DATE_CREATE", "") < flt[">=DATE_CREATE"]:
                continue
            yield row

    def list_paged(self, method, params):
        self.calls.append((method, params))
        if method == "user.get":
            yield {"ID": 32, "NAME": "Иван", "LAST_NAME": "Петров",
                   "ACTIVE": "Y", "UF_DEPARTMENT": [44]}
            return
        if method == "crm.stagehistory.list":
            owners = params.get("filter", {}).get("OWNER_ID")
            owners = owners if isinstance(owners, list) else [owners]
            for owner in owners:
                yield from self.history.get(int(owner or 0), [])
            return
        return
        yield  # pragma: no cover


def _deal(deal_id, *, stage="C18:NEW", semantic="P", created="2026-08-01T10:00:00+03:00",
          modified="2026-08-01T10:00:00+03:00", opportunity="100000", category=18, lead_id=0):
    return {
        "ID": str(deal_id), "TITLE": f"Сделка {deal_id}", "CATEGORY_ID": str(category),
        "STAGE_ID": stage, "STAGE_SEMANTIC_ID": semantic, "ASSIGNED_BY_ID": "32",
        "SOURCE_ID": "CALL", "OPPORTUNITY": opportunity, "CURRENCY_ID": "RUB",
        "DATE_CREATE": created, "DATE_MODIFY": modified, "CLOSED": "N",
        "LEAD_ID": str(lead_id) if lead_id else "",
    }


def _lead(lead_id, *, status="NEW", created="2026-08-01T09:00:00+03:00",
          modified="2026-08-01T09:00:00+03:00"):
    return {
        "ID": str(lead_id), "TITLE": f"Лид {lead_id}", "STATUS_ID": status,
        "SOURCE_ID": "CALL", "ASSIGNED_BY_ID": "32",
        "DATE_CREATE": created, "DATE_MODIFY": modified,
    }


@pytest.fixture
def fake_client(monkeypatch):
    holder = {}

    def _install(client):
        holder["client"] = client
        monkeypatch.setattr(etl, "BitrixClient", lambda *a, **kw: client)
        return client

    return _install


def test_backfill_loads_facts_dimensions_and_stage_history(analytics_db, fake_client):
    fake_client(FakeClient(
        deals=[_deal(101), _deal(102, stage="C18:WON", semantic="S")],
        leads=[_lead(201), _lead(202, status="CONVERTED")],
        history={101: [{"ID": 1, "OWNER_ID": 101, "STAGE_ID": "C18:NEW",
                        "CREATED_TIME": "2026-08-01T10:00:00+03:00", "CATEGORY_ID": 18}]},
    ))
    summary = etl.run_sync("backfill", since_override="2026-01-01")

    assert summary["deals"] == 2 and summary["leads"] == 2
    with analytics_session(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dim_pipeline").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM dim_stage").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM dim_user").fetchone()[0] == 1
        assert conn.execute(
            "SELECT department_name FROM dim_user WHERE user_id = 32"
        ).fetchone()[0] == "Отдел Трофимовой"

        won = conn.execute("SELECT is_won, is_lost FROM fact_deal WHERE deal_id = 102").fetchone()
        assert (won["is_won"], won["is_lost"]) == (1, 0)

        # У сделки 102 истории нет — событие должно быть синтезировано,
        # иначе она исчезнет из воронки.
        stages = dict(conn.execute(
            "SELECT entity_id, COUNT(*) FROM fact_stage_event "
            "WHERE entity_type='deal' GROUP BY entity_id"
        ).fetchall())
        assert stages == {101: 1, 102: 1}


def test_incremental_only_pulls_changed_records(analytics_db, fake_client):
    client = fake_client(FakeClient(deals=[_deal(101)], leads=[_lead(201)]))
    etl.run_sync("backfill", since_override="2026-01-01")

    client.deals.append(_deal(103, modified="2099-01-01T00:00:00+03:00"))
    client.calls.clear()
    etl.run_sync("incremental")

    deal_filters = [
        params.get("filter", {}) for method, params in client.calls
        if method == "crm.deal.list"
    ]
    assert any(">=DATE_MODIFY" in f for f in deal_filters), "инкремент обязан ставить watermark"

    with analytics_session(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_deal").fetchone()[0] == 2


def test_full_sync_marks_deleted_deals(analytics_db, fake_client):
    client = fake_client(FakeClient(deals=[_deal(101), _deal(102)], leads=[]))
    etl.run_sync("backfill", since_override="2026-01-01")

    # Сделку 102 удалили в Bitrix — она просто перестала приходить.
    client.deals = [_deal(101)]
    etl.run_sync("full", since_override="2026-01-01")

    with analytics_session(readonly=True) as conn:
        assert conn.execute(
            "SELECT is_deleted FROM fact_deal WHERE deal_id = 102"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_stage_event WHERE entity_id = 102"
        ).fetchone()[0] == 0


def test_leads_are_linked_to_their_deals(analytics_db, fake_client):
    fake_client(FakeClient(
        deals=[_deal(101, lead_id=201)],
        leads=[_lead(201, status="CONVERTED")],
    ))
    etl.run_sync("backfill", since_override="2026-01-01")

    with analytics_session(readonly=True) as conn:
        row = conn.execute(
            "SELECT is_converted, converted_deal_id FROM fact_lead WHERE lead_id = 201"
        ).fetchone()
        assert row["is_converted"] == 1
        assert row["converted_deal_id"] == 101


def test_etl_run_is_journalled(analytics_db, fake_client):
    fake_client(FakeClient(deals=[_deal(101)], leads=[]))
    etl.run_sync("backfill", since_override="2026-01-01")
    with analytics_session(readonly=True) as conn:
        row = conn.execute(
            "SELECT kind, status, rows_upserted FROM etl_run ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (row["kind"], row["status"]) == ("backfill", "ok")
        assert row["rows_upserted"] == 1


def test_repeated_run_is_idempotent(analytics_db, fake_client):
    fake_client(FakeClient(
        deals=[_deal(101)], leads=[],
        history={101: [{"ID": 1, "OWNER_ID": 101, "STAGE_ID": "C18:NEW",
                        "CREATED_TIME": "2026-08-01T10:00:00+03:00", "CATEGORY_ID": 18}]},
    ))
    etl.run_sync("backfill", since_override="2026-01-01")
    etl.run_sync("backfill", since_override="2026-01-01")

    with analytics_session(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_deal").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_stage_event").fetchone()[0] == 1


def test_window_since_is_frozen_after_first_run(analytics_db, fake_client):
    """Пересчёт окна на каждом прогоне разошёлся бы с уже загруженными данными."""
    fake_client(FakeClient(deals=[], leads=[]))
    etl.run_sync("backfill", since_override="2026-01-01")
    etl.run_sync("incremental")
    with analytics_session(readonly=True) as conn:
        assert conn.execute(
            "SELECT value FROM analytics_meta WHERE key = 'window_since'"
        ).fetchone()[0] == "2026-01-01"


@pytest.mark.parametrize("stage_id,code,expected", [
    ("C18:WON", "S", "won"),
    ("C18:APOLOGY", "F", "lost"),
    ("C18:NEW", "P", "in_progress"),
    ("C18:WON", None, "won"),
    ("C18:LOSE", None, "lost"),
    ("C18:UC_UFPFKK", None, "in_progress"),
])
def test_stage_semantic_inference(stage_id, code, expected):
    assert etl.infer_semantic(stage_id, code) == expected


def test_stage_semantic_override_wins():
    """Воронка может объявить успешной произвольную стадию — по имени не видно."""
    assert etl.infer_semantic("C18:UC_RUCRAH", "P", {"C18:UC_RUCRAH": "won"}) == "won"
