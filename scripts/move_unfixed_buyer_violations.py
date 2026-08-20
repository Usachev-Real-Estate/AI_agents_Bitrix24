"""One-off: after 14:00, move still-violating buyer deals to Общая база.

Excludes: lost, ofer, Абзалилов, Логутина, Орешникова.
Does not re-enable GENERAL_BASE_MOVE_AFTER. Honors DRY_RUN.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import get_settings, setup_logging  # noqa: E402
from notify import _bx_call_sync  # noqa: E402
from tools import (  # noqa: E402
    GENERAL_BASE_BUYERS_STAGE_ID,
    GENERAL_BASE_CATEGORY_ID,
    _as_list,
    _build_crm_link,
    _build_rop_map,
    _bx_get_all_sync,
    _clean_str,
    _coerce_int,
    check_buyer_deal_violations,
    get_deals_by_funnel_with_timeline,
)

MSK = ZoneInfo("Europe/Moscow")
SKIP_PEOPLE = {
    "Марат Абзалилов",
    "Ирина Логутина",
    "Марина Орешникова",
}
SKIP_RULES = {"buyer_lost_no_reason", "buyer_ofer_comment"}
LOG_PATH = ROOT / "logs" / "buyer-violations-2026-08-14.log"


def parse_watch_ids() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    in_deals = False
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("===== DEALS ====="):
            in_deals = True
            continue
        if not in_deals or not line.startswith("• #"):
            continue
        parts = [p.strip() for p in line[2:].split("|")]
        did_s, person, stage, rule, _flag = parts
        did = int(did_s.replace("#", ""))
        if person in SKIP_PEOPLE or rule in SKIP_RULES:
            continue
        rows.append({"id": did, "person_log": person, "stage": stage, "rule": rule})
    return rows


def user_names(uids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    raw = _bx_get_all_sync("user.get", {})
    for user in raw if isinstance(raw, list) else _as_list(raw):
        uid = _coerce_int(user.get("ID"))
        if uid in uids:
            full = f"{_clean_str(user.get('NAME'))} {_clean_str(user.get('LAST_NAME'))}".strip()
            names[uid] = full or f"ID:{uid}"
    return names


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    now = datetime.now(MSK)
    print(f"now={now.isoformat()} dry_run={settings.dry_run}")
    if now.hour < 14:
        raise SystemExit(f"too early: {now.strftime('%H:%M')} MSK")
    if settings.dry_run:
        raise SystemExit("DRY_RUN=true — refuse to move")

    watch = parse_watch_ids()
    watch_ids = {r["id"] for r in watch}
    print(f"watch={len(watch_ids)}")

    payload = get_deals_by_funnel_with_timeline.invoke(
        {"category_id": settings.buyers_category_id},
    )
    deals = [
        d for d in (payload.get("deals") or [])
        if _coerce_int(d.get("deal_id")) in watch_ids
    ]
    print(f"still in buyers funnel among watch={len(deals)}")

    current_time = datetime.now(timezone.utc).isoformat()
    violations = check_buyer_deal_violations(deals, current_time, _build_rop_map())
    still: dict[int, dict[str, Any]] = {}
    for v in violations:
        rule = str(v.get("rule") or "")
        if rule in SKIP_RULES:
            continue
        did = _coerce_int(v.get("entity_id"))
        if did not in watch_ids:
            continue
        still.setdefault(did, v)

    names = user_names({
        _coerce_int(v.get("responsible_id")) for v in still.values()
    })
    skip_uids = {uid for uid, name in names.items() if name in SKIP_PEOPLE}

    to_move: list[dict[str, Any]] = []
    skipped_people: list[int] = []
    for did, v in sorted(still.items()):
        uid = _coerce_int(v.get("responsible_id"))
        name = names.get(uid, f"ID:{uid}")
        if name in SKIP_PEOPLE or uid in skip_uids:
            skipped_people.append(did)
            print(f"SKIP person {did} {name}")
            continue
        to_move.append(v)

    # Сделки, ушедшие из воронки покупателей или переставшие нарушать.
    present_ids = {_coerce_int(d.get("deal_id")) for d in deals}
    left_funnel = sorted(watch_ids - present_ids)
    still_ok = sorted(present_ids - set(still))

    print(f"still_violating={len(still)} to_move={len(to_move)} "
          f"skip_people={len(skipped_people)} fixed_on_stage={len(still_ok)} "
          f"left_funnel={len(left_funnel)}")

    moved = 0
    fails: list[tuple[int, str]] = []
    by_user: dict[str, list[int]] = defaultdict(list)
    for v in to_move:
        did = _coerce_int(v.get("entity_id"))
        uid = _coerce_int(v.get("responsible_id"))
        name = names.get(uid, f"ID:{uid}")
        try:
            _bx_call_sync(
                "crm.item.update",
                {
                    "entityTypeId": 2,
                    "id": did,
                    "fields": {
                        "categoryId": GENERAL_BASE_CATEGORY_ID,
                        "stageId": GENERAL_BASE_BUYERS_STAGE_ID,
                    },
                },
            )
            moved += 1
            by_user[name].append(did)
            print(f"MOVED {did} {name} {v.get('rule')}")
        except Exception as exc:
            fails.append((did, type(exc).__name__))
            print(f"FAIL {did} {type(exc).__name__}")

    print("===== FIXED (still on buyers, no violation) =====")
    for did in still_ok:
        print(f"OK #{did} {_build_crm_link('deal', did)}")
    print("===== LEFT FUNNEL ALREADY =====")
    for did in left_funnel:
        print(f"LEFT #{did}")
    print("===== SKIP ORESHNIKOVA ETC =====")
    for did in skipped_people:
        print(f"SKIP #{did}")
    by_user_counts = dict(Counter({k: len(v) for k, v in by_user.items()}))
    print(
        f"DONE moved={moved} fail={len(fails)} by_user={by_user_counts}"
    )
    if fails:
        print("FAILS", fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
