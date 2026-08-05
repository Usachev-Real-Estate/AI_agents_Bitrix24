"""Fill buyer-deal «Базовая ставка» from broker motivation CSV."""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Settings, get_settings
from notify import _bx_call_sync
from tools import _as_list, _bx_get_all_sync, _coerce_int

logger = logging.getLogger(__name__)

UF_BASE_RATE = "UF_CRM_1785315943350"
DEFAULT_CSV = Path(
    "data/Мотивация брокеров 3 кв 2026 - Мотивация брокеров 3 кв 2026.csv"
)
ROP_SUFFIX_RE = re.compile(r"\s*\(роп\)\s*", re.IGNORECASE)
WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CsvBrokerRate:
    """One broker row from the motivation CSV."""

    fio: str
    rate: str
    rop: str = ""


@dataclass
class FillSummary:
    """Outcome counters for a fill run."""

    matched_brokers: int = 0
    unmatched_csv: list[str] = field(default_factory=list)
    deals_total: int = 0
    updated: int = 0
    would_update: int = 0
    skipped_same: int = 0
    skipped_unknown_assignee: int = 0
    unknown_assignee_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True
    broker_rates: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialize summary for CLI/JSON output."""
        return {
            "dry_run": self.dry_run,
            "matched_brokers": self.matched_brokers,
            "unmatched_csv": self.unmatched_csv,
            "deals_total": self.deals_total,
            "updated": self.updated,
            "would_update": self.would_update,
            "skipped_same": self.skipped_same,
            "skipped_unknown_assignee": self.skipped_unknown_assignee,
            "unknown_assignee_ids": self.unknown_assignee_ids,
            "errors": self.errors,
            "broker_rates": {str(k): v for k, v in sorted(self.broker_rates.items())},
        }


def normalize_person_name(value: str) -> str:
    """Normalize a person name for matching.

    Args:
        value: Raw FIO from CSV or Bitrix.

    Returns:
        Lowercased name without ROP suffix, ё→е, collapsed spaces.
    """
    text = (value or "").strip().lower().replace("ё", "е")
    text = ROP_SUFFIX_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def name_key_variants(value: str) -> set[str]:
    """Build match keys for Last+First and First+Last orders.

    Args:
        value: Raw or normalized person name.

    Returns:
        Set of normalized two-token keys (and full token join).
    """
    norm = normalize_person_name(value)
    if not norm:
        return set()
    parts = norm.split()
    keys = {norm}
    if len(parts) >= 2:
        last, first = parts[0], parts[1]
        keys.add(f"{last} {first}")
        keys.add(f"{first} {last}")
        # Bitrix may store LAST NAME first; CSV may put given name first.
        if len(parts) >= 3:
            keys.add(f"{parts[0]} {parts[1]}")
            keys.add(f"{parts[-2]} {parts[-1]}")
            keys.add(f"{parts[1]} {parts[0]}")
    return keys


def load_csv_rates(csv_path: Path) -> list[CsvBrokerRate]:
    """Load broker base rates from motivation CSV.

    Args:
        csv_path: Path to CSV with columns ФИО, РОП, Базовая ставка.

    Returns:
        List of broker rate rows (rate kept as in CSV, including '-').
    """
    rows: list[CsvBrokerRate] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            fio = (raw.get("ФИО") or "").strip()
            if not fio:
                continue
            rate = (raw.get("Базовая ставка") or "").strip()
            rop = (raw.get("РОП") or "").strip()
            rows.append(CsvBrokerRate(fio=fio, rate=rate, rop=rop))
    return rows


def _user_display_name(user: dict[str, Any]) -> str:
    """Build display name from Bitrix user fields."""
    parts = [
        str(user.get("LAST_NAME") or "").strip(),
        str(user.get("NAME") or "").strip(),
        str(user.get("SECOND_NAME") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _user_is_active(user: dict[str, Any]) -> bool:
    """Return True if Bitrix user is active."""
    active = user.get("ACTIVE")
    return active is True or active == "Y" or active == 1 or active == "1"


def build_broker_rate_map(
    csv_rows: list[CsvBrokerRate],
    users: list[dict[str, Any]],
) -> tuple[dict[int, str], list[str], dict[int, str]]:
    """Map Bitrix user IDs to base rates from CSV.

    Prefers ACTIVE users when several accounts share the same name.

    Args:
        csv_rows: Parsed CSV rows.
        users: Bitrix user.get records.

    Returns:
        Tuple of (user_id→rate, unmatched CSV FIOs, user_id→display name).
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for user in users:
        display = _user_display_name(user)
        for key in name_key_variants(display):
            index.setdefault(key, []).append(user)
        # Also index NAME LAST_NAME only (without patronymic).
        last = str(user.get("LAST_NAME") or "").strip()
        first = str(user.get("NAME") or "").strip()
        if last and first:
            for key in name_key_variants(f"{last} {first}"):
                index.setdefault(key, []).append(user)

    rate_by_user: dict[int, str] = {}
    names_by_user: dict[int, str] = {}
    unmatched: list[str] = []

    for row in csv_rows:
        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for key in name_key_variants(row.fio):
            for user in index.get(key, []):
                uid = _coerce_int(user.get("ID"))
                if uid <= 0 or uid in seen_ids:
                    continue
                seen_ids.add(uid)
                candidates.append(user)

        if not candidates:
            unmatched.append(row.fio)
            continue

        active = [u for u in candidates if _user_is_active(u)]
        chosen = active[0] if active else candidates[0]
        uid = _coerce_int(chosen.get("ID"))
        rate_by_user[uid] = row.rate
        names_by_user[uid] = _user_display_name(chosen)
        logger.info(
            "Mapped CSV %r -> user #%s (%s) rate=%r",
            row.fio,
            uid,
            names_by_user[uid],
            row.rate,
        )

    return rate_by_user, unmatched, names_by_user


@dataclass(frozen=True)
class DealUpdatePlan:
    """One planned deal field update."""

    deal_id: int
    assigned_by_id: int
    new_rate: str
    old_rate: str


def plan_deal_updates(
    deals: list[dict[str, Any]],
    rate_by_user: dict[int, str],
) -> tuple[list[DealUpdatePlan], list[DealUpdatePlan], list[int]]:
    """Decide which deals need UF_BASE_RATE updates.

    Args:
        deals: Deal records with ID, ASSIGNED_BY_ID, UF field.
        rate_by_user: Mapped broker rates.

    Returns:
        (to_update, already_same, unknown_assignee_ids unique).
    """
    to_update: list[DealUpdatePlan] = []
    same: list[DealUpdatePlan] = []
    unknown: list[int] = []
    seen_unknown: set[int] = set()

    for deal in deals:
        deal_id = _coerce_int(deal.get("ID"))
        assigned = _coerce_int(deal.get("ASSIGNED_BY_ID"))
        old_rate = str(deal.get(UF_BASE_RATE) or "").strip()
        if assigned not in rate_by_user:
            if assigned > 0 and assigned not in seen_unknown:
                seen_unknown.add(assigned)
                unknown.append(assigned)
            continue
        new_rate = rate_by_user[assigned]
        plan = DealUpdatePlan(
            deal_id=deal_id,
            assigned_by_id=assigned,
            new_rate=new_rate,
            old_rate=old_rate,
        )
        if old_rate == new_rate:
            same.append(plan)
        else:
            to_update.append(plan)

    return to_update, same, unknown


def update_deal_base_rate(
    deal_id: int,
    rate: str,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Set UF_BASE_RATE on a deal (honors dry_run).

    Args:
        deal_id: Bitrix deal ID.
        rate: Value to write (e.g. '40%' or '-').
        dry_run: If True, skip mutation.

    Returns:
        Result dict with dry_run_skipped or ok flag.
    """
    if dry_run:
        logger.info(
            "DRY_RUN: would set deal #%s %s=%r",
            deal_id,
            UF_BASE_RATE,
            rate,
        )
        return {"dry_run_skipped": True, "deal_id": deal_id, "rate": rate}

    result = _bx_call_sync(
        "crm.deal.update",
        {"id": deal_id, "fields": {UF_BASE_RATE: rate}},
    )
    return {"ok": bool(result), "deal_id": deal_id, "rate": rate}


def fetch_users() -> list[dict[str, Any]]:
    """Load all Bitrix users."""
    raw = _bx_get_all_sync("user.get", {})
    return raw if isinstance(raw, list) else _as_list(raw)


def fetch_buyer_deals(category_id: int) -> list[dict[str, Any]]:
    """Load all deals in buyers funnel.

    Args:
        category_id: Bitrix deal category ID (buyers).

    Returns:
        Deal dicts.
    """
    raw = _bx_get_all_sync(
        "crm.deal.list",
        {
            "filter": {"CATEGORY_ID": category_id},
            "select": ["ID", "ASSIGNED_BY_ID", "CLOSED", "STAGE_ID", UF_BASE_RATE],
        },
    )
    return raw if isinstance(raw, list) else _as_list(raw)


def run_fill(
    settings: Settings | None = None,
    *,
    csv_path: Path = DEFAULT_CSV,
    dry_run: bool | None = None,
    limit: int | None = None,
) -> FillSummary:
    """Fill base rate on buyer deals from CSV.

    Args:
        settings: App settings (category id, dry_run default).
        csv_path: Motivation CSV path.
        dry_run: Override settings.dry_run when not None.
        limit: Optional max number of updates to apply/preview.

    Returns:
        FillSummary with counters.
    """
    settings = settings or get_settings()
    is_dry = settings.dry_run if dry_run is None else dry_run
    summary = FillSummary(dry_run=is_dry)

    csv_rows = load_csv_rates(csv_path)
    users = fetch_users()
    rate_by_user, unmatched, _names = build_broker_rate_map(csv_rows, users)
    summary.matched_brokers = len(rate_by_user)
    summary.unmatched_csv = unmatched
    summary.broker_rates = dict(rate_by_user)

    if unmatched:
        logger.warning("CSV brokers not found in Bitrix: %s", unmatched)

    category_id = settings.buyers_category_id or 18
    deals = fetch_buyer_deals(category_id)
    summary.deals_total = len(deals)

    to_update, same, unknown = plan_deal_updates(deals, rate_by_user)
    summary.skipped_same = len(same)
    summary.skipped_unknown_assignee = sum(
        1
        for d in deals
        if _coerce_int(d.get("ASSIGNED_BY_ID")) not in rate_by_user
    )
    summary.unknown_assignee_ids = unknown

    if limit is not None:
        to_update = to_update[: max(0, limit)]

    for plan in to_update:
        try:
            result = update_deal_base_rate(
                plan.deal_id,
                plan.new_rate,
                dry_run=is_dry,
            )
            if result.get("dry_run_skipped"):
                summary.would_update += 1
            elif result.get("ok"):
                summary.updated += 1
            else:
                summary.errors.append(
                    f"deal #{plan.deal_id}: unexpected response {result!r}"
                )
        except Exception as exc:  # noqa: BLE001 — collect and continue
            msg = f"deal #{plan.deal_id}: {exc}"
            logger.exception("Failed to update %s", msg)
            summary.errors.append(msg)

    logger.info(
        "Fill done dry_run=%s matched=%s would_update=%s updated=%s "
        "same=%s unknown=%s errors=%s",
        is_dry,
        summary.matched_brokers,
        summary.would_update,
        summary.updated,
        summary.skipped_same,
        summary.skipped_unknown_assignee,
        len(summary.errors),
    )
    return summary
