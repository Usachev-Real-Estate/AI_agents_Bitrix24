#!/usr/bin/env python3
"""Fill NEW/SHARED leads from CIAN analytics CSV (dry-run by default).

Mapping:
  TITLE              ← ЖК  (для аренды: «Аренда — {ЖК}»)
  OPPORTUNITY        ← sale: 3% of «Цена объекта»; rent: сумма аренды (RUB)
  UF_CRM_1780911032  ← ID Афины  («ID объекта Афины»)
  timeline comment   ← Ссылка на объект
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import get_settings, setup_logging  # noqa: E402
from notify import _bx_call_sync  # noqa: E402
from tools import (  # noqa: E402
    LEAD_STATUS_NEW,
    LEAD_STATUS_SHARED,
    _as_list,
    _build_crm_link,
    _bx_get_all_sync,
    _coerce_int,
)

logger = logging.getLogger(__name__)

DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "Циан Аналитика УСАЧЁВЪ - Все звонки 2026-05-01 - 2026-08-10.csv"
)
AFINA_UF = "UF_CRM_1780911032"
COMMISSION_RATE = 0.03
CURRENCY_ID = "RUB"


@dataclass
class CsvObject:
    date: str
    phone_norm: str
    afina_id: str
    price: int | None
    is_rent: bool
    link: str
    jk: str
    cian_id: str


@dataclass
class PlannedUpdate:
    lead_id: int
    status: str
    phone: str
    title_now: str
    title_new: str
    opportunity_now: str
    opportunity_new: float | None
    afina_now: str
    afina_new: str
    link: str
    is_rent: bool
    skip_reason: str = ""


@dataclass
class Summary:
    dry_run: bool = True
    csv_rows: int = 0
    csv_phones: int = 0
    leads_scanned: int = 0
    matched_leads: int = 0
    planned: int = 0
    skipped: int = 0
    updated: int = 0
    comments_added: int = 0
    errors: list[str] = field(default_factory=list)
    preview: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        return digits[1:]
    if len(digits) == 10:
        return digits
    if len(digits) > 10:
        return digits[-10:]
    return digits


def parse_price(raw: str) -> tuple[int | None, bool]:
    s = (raw or "").strip()
    if not s:
        return None, False
    is_rent = "мес" in s.lower()
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None, is_rent
    return int(digits), is_rent


def row_score(obj: CsvObject) -> tuple[int, str]:
    """Prefer rows with price + afina + jk; then latest date."""
    score = 0
    if obj.price is not None:
        score += 4
    if obj.afina_id:
        score += 2
    if obj.jk:
        score += 2
    if obj.link:
        score += 1
    if not obj.is_rent:
        score += 1
    return score, obj.date


def load_csv(path: Path) -> tuple[list[dict[str, str]], dict[str, CsvObject]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if line.startswith("Дата,"))
    rows = list(csv.DictReader(lines[idx:]))
    best: dict[str, CsvObject] = {}
    for r in rows:
        phone = norm_phone(r.get("Телефон") or "")
        if len(phone) < 10:
            continue
        price, is_rent = parse_price(r.get("Цена объекта") or "")
        obj = CsvObject(
            date=(r.get("Дата") or "").strip(),
            phone_norm=phone,
            afina_id=(r.get("ID Афины") or "").strip(),
            price=price,
            is_rent=is_rent,
            link=(r.get("Ссылка на объект") or "").strip(),
            jk=(r.get("ЖК") or "").strip(),
            cian_id=(r.get("ID Циан") or "").strip(),
        )
        prev = best.get(phone)
        if prev is None or row_score(obj) > row_score(prev):
            best[phone] = obj
    return rows, best


def fetch_target_leads() -> list[dict[str, Any]]:
    raw = _bx_get_all_sync(
        "crm.lead.list",
        {
            "filter": {
                "STATUS_ID": [LEAD_STATUS_NEW, LEAD_STATUS_SHARED],
                "HAS_PHONE": "Y",
            },
            "select": [
                "ID",
                "TITLE",
                "STATUS_ID",
                "ASSIGNED_BY_ID",
                "PHONE",
                "OPPORTUNITY",
                "CURRENCY_ID",
                AFINA_UF,
            ],
        },
    )
    return raw if isinstance(raw, list) else _as_list(raw)


def plan_updates(
    leads: list[dict[str, Any]],
    by_phone: dict[str, CsvObject],
) -> list[PlannedUpdate]:
    planned: list[PlannedUpdate] = []
    seen_leads: set[int] = set()
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        lead_id = _coerce_int(lead.get("ID"))
        if not lead_id or lead_id in seen_leads:
            continue
        phones = lead.get("PHONE") or []
        if isinstance(phones, dict):
            phones = [phones]
        matched_obj: CsvObject | None = None
        matched_phone = ""
        for p in phones if isinstance(phones, list) else []:
            raw = str(p.get("VALUE") if isinstance(p, dict) else p or "")
            n = norm_phone(raw)
            if n in by_phone:
                candidate = by_phone[n]
                if matched_obj is None or row_score(candidate) > row_score(matched_obj):
                    matched_obj = candidate
                    matched_phone = n
        if matched_obj is None:
            continue
        seen_leads.add(lead_id)

        if matched_obj.price is None:
            opp_new = None
        elif matched_obj.is_rent:
            # Аренда: в сумму пишем цену аренды (не 3%)
            opp_new = float(matched_obj.price)
        else:
            opp_new = round(matched_obj.price * COMMISSION_RATE, 2)

        title_new = matched_obj.jk
        if matched_obj.is_rent:
            # В названии явно указываем аренду
            if title_new:
                title_new = f"Аренда — {title_new}"
            else:
                title_new = "Аренда"

        skip = ""
        if not title_new and opp_new is None and not matched_obj.afina_id and not matched_obj.link:
            skip = "empty_csv_payload"
        planned.append(
            PlannedUpdate(
                lead_id=lead_id,
                status=str(lead.get("STATUS_ID") or ""),
                phone="+7" + matched_phone,
                title_now=str(lead.get("TITLE") or ""),
                title_new=title_new,
                opportunity_now=str(lead.get("OPPORTUNITY") or ""),
                opportunity_new=opp_new,
                afina_now=str(lead.get(AFINA_UF) or "").strip(),
                afina_new=matched_obj.afina_id,
                link=matched_obj.link,
                is_rent=matched_obj.is_rent,
                skip_reason=skip,
            )
        )
    return planned


def apply_update(item: PlannedUpdate, *, dry_run: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if item.title_new:
        fields["TITLE"] = item.title_new
    if item.opportunity_new is not None:
        fields["OPPORTUNITY"] = item.opportunity_new
        fields["CURRENCY_ID"] = CURRENCY_ID
        fields["IS_MANUAL_OPPORTUNITY"] = "Y"
    if item.afina_new:
        fields[AFINA_UF] = item.afina_new

    if not fields and not item.link:
        return {"skipped": True, "reason": "nothing_to_write"}

    if dry_run:
        logger.info(
            "DRY_RUN lead #%s fields=%s comment=%s",
            item.lead_id,
            fields,
            (item.link[:80] + "…") if len(item.link) > 80 else item.link,
        )
        return {
            "dry_run_skipped": True,
            "lead_id": item.lead_id,
            "fields": fields,
            "comment": item.link,
        }

    if fields:
        _bx_call_sync("crm.lead.update", {"id": item.lead_id, "fields": fields})
    comment_ok = False
    if item.link:
        _bx_call_sync(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_TYPE": "lead",
                    "ENTITY_ID": item.lead_id,
                    "COMMENT": item.link,
                }
            },
        )
        comment_ok = True
    return {
        "ok": True,
        "lead_id": item.lead_id,
        "fields": fields,
        "comment": comment_ok,
    }


def run(
    csv_path: Path,
    *,
    dry_run: bool,
    limit: int | None = None,
    include_rent: bool = True,
    skip_filled_afina: bool = False,
) -> Summary:
    summary = Summary(dry_run=dry_run)
    rows, by_phone = load_csv(csv_path)
    summary.csv_rows = len(rows)
    summary.csv_phones = len(by_phone)

    leads = fetch_target_leads()
    summary.leads_scanned = len(leads)
    planned = plan_updates(leads, by_phone)
    summary.matched_leads = len(planned)

    actionable: list[PlannedUpdate] = []
    for item in planned:
        if item.skip_reason:
            summary.skipped += 1
            continue
        if item.is_rent and not include_rent:
            item.skip_reason = "rent_skipped"
            summary.skipped += 1
            continue
        if skip_filled_afina and item.afina_now:
            item.skip_reason = "afina_already_filled"
            summary.skipped += 1
            continue
        actionable.append(item)

    if limit is not None:
        actionable = actionable[:limit]

    summary.planned = len(actionable)
    summary.preview = [
        {
            "lead_id": x.lead_id,
            "status": x.status,
            "phone": x.phone,
            "title": f"{x.title_now!r} → {x.title_new!r}",
            "opportunity": f"{x.opportunity_now} → {x.opportunity_new}",
            "afina": f"{x.afina_now!r} → {x.afina_new!r}",
            "link": x.link,
            "rent": x.is_rent,
            "crm": _build_crm_link("lead", x.lead_id),
        }
        for x in actionable
    ]

    for item in actionable:
        try:
            result = apply_update(item, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — one-off script, keep going
            msg = f"lead #{item.lead_id}: {exc}"
            logger.exception(msg)
            summary.errors.append(msg)
            continue
        if result.get("dry_run_skipped") or result.get("ok"):
            if not dry_run and result.get("ok"):
                summary.updated += 1
                if result.get("comment"):
                    summary.comments_added += 1
        elif result.get("skipped"):
            summary.skipped += 1

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill NEW/SHARED leads from CIAN CSV (TITLE/OPPORTUNITY/Afina/timeline)"
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--skip-rent",
        action="store_true",
        help="Skip rows with monthly rent price",
    )
    parser.add_argument(
        "--skip-filled-afina",
        action="store_true",
        help="Skip leads that already have ID объекта Афины filled",
    )
    parser.add_argument(
        "--preview-file",
        type=Path,
        default=Path("data/cian_leads_fill_preview.json"),
    )
    args = parser.parse_args()
    if args.dry_run and args.live:
        parser.error("Use either --dry-run or --live, not both")

    settings = get_settings()
    setup_logging(settings.log_level)
    dry_run = True
    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = bool(settings.dry_run)

    if not dry_run:
        logger.warning("LIVE mode: will update leads in Bitrix24")

    summary = run(
        args.csv,
        dry_run=dry_run,
        limit=args.limit,
        include_rent=not args.skip_rent,
        skip_filled_afina=args.skip_filled_afina,
    )
    args.preview_file.parent.mkdir(parents=True, exist_ok=True)
    args.preview_file.write_text(
        json.dumps(summary.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(
        {k: v for k, v in summary.as_dict().items() if k != "preview"},
        ensure_ascii=False,
        indent=2,
    ))
    print(f"preview_file={args.preview_file} items={len(summary.preview)}")
    for row in summary.preview[:15]:
        print(
            f"#{row['lead_id']} {row['phone']} | {row['title']} | "
            f"opp {row['opportunity']} | afina {row['afina']}"
        )
        print(f"  {row['crm']}")
    if len(summary.preview) > 15:
        print(f"... and {len(summary.preview) - 15} more")


if __name__ == "__main__":
    main()
