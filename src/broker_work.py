"""Доказательства работы брокера по клиенту.

Правило агентства: пока клиент выбирает и смотрит объекты, работа брокера
должна быть видна в карточке. Видна она может быть тремя способами, по
убыванию надёжности:

1. Звонок с клиентом — первоисточник, спорить не о чем.
2. Звонков нет → нужен развёрнутый комментарий, по которому понятно, что
   происходит с клиентом.
3. Комментарий вида «написал клиенту» → нужен скриншот переписки. Иначе
   правило вырождается: «написал» пишется за две секунды и ничего не
   доказывает, а именно такие формулировки и появляются, когда за наличие
   комментария начинают спрашивать.

Всё, что можно посчитать, считается здесь, а не спрашивается у модели:
вывод «брокер не работал N дней» — это претензия к человеку, и она должна
опираться на факты из CRM, а не на впечатление языковой модели. У модели
спрашивается только то, что без чтения текста не узнать: утверждает ли
брокер, что писал клиенту, и понятна ли из его комментария картина.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from funnel_profiles import FunnelProfile

CALL_ACTIVITY_TYPE_ID = 2

# Чем подтверждена работа.
PROVEN_BY_CALL = "call"
PROVEN_BY_SCREENSHOT = "screenshot"
PROVEN_BY_COMMENT = "comment"
# Чем не подтверждена.
GAP_CLAIMED_MESSAGE = "claimed_message_no_proof"
GAP_EMPTY_COMMENT = "comment_says_nothing"
GAP_NO_TRACE = "no_trace"
GAP_OUT_OF_WINDOW = "window_not_started"

PROVEN = frozenset({PROVEN_BY_CALL, PROVEN_BY_SCREENSHOT, PROVEN_BY_COMMENT})

REASON_RU: dict[str, str] = {
    PROVEN_BY_CALL: "есть звонок с клиентом",
    PROVEN_BY_SCREENSHOT: "написал клиенту, приложен скриншот переписки",
    PROVEN_BY_COMMENT: "есть развёрнутый комментарий",
    GAP_CLAIMED_MESSAGE: "брокер пишет, что написал клиенту, но скриншота переписки нет",
    GAP_EMPTY_COMMENT: "комментарии есть, но по ним не понять, что с клиентом",
    GAP_NO_TRACE: "следов работы нет",
    GAP_OUT_OF_WINDOW: "срок ещё не наступил",
}


def work_window_days(profile: FunnelProfile, stage_id: str) -> int:
    """Сколько дней у брокера есть на след работы по этому этапу."""
    table = profile.work_window_days
    if stage_id in table:
        return int(table[stage_id])
    return int(table.get("_default", 7))


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def events_in_window(
    events: list[dict[str, Any]],
    now: datetime,
    days: int,
) -> list[dict[str, Any]]:
    """События за последние ``days`` дней.

    Событие с неразбираемой датой считается свежим: пропустить работу брокера
    хуже, чем лишний раз её засчитать.
    """
    edge = now - timedelta(days=days)
    fresh: list[dict[str, Any]] = []
    for event in events:
        created = _parse(event.get("created"))
        if created is None or created >= edge:
            fresh.append(event)
    return fresh


def _has_call(events: list[dict[str, Any]]) -> bool:
    """Состоявшийся звонок либо расшифровка разговора."""
    for event in events:
        if event.get("kind") == "transcript":
            return True
        if event.get("kind") != "activity":
            continue
        if int(event.get("type_id") or 0) != CALL_ACTIVITY_TYPE_ID:
            continue
        # Незакрытое дело «позвонить» — это план, а не звонок.
        if str(event.get("completed") or "").upper() == "Y":
            return True
    return False


def assess_broker_work(
    events: list[dict[str, Any]],
    *,
    profile: FunnelProfile,
    stage_id: str,
    hours_on_stage: float | None,
    claims_messaged: bool,
    comment_informative: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Подтверждена ли работа брокера за последнее окно по этапу.

    Возвращает {proven, reason, window_days, days_quiet}. days_quiet — дней с
    последнего любого следа в карточке; None, если следов нет вовсе.
    """
    now = now or datetime.now(timezone.utc)
    days = work_window_days(profile, stage_id)
    window = events_in_window(events, now, days)

    last_seen: datetime | None = None
    for event in events:
        created = _parse(event.get("created"))
        if created is not None and (last_seen is None or created > last_seen):
            last_seen = created
    days_quiet = (
        (now - last_seen).total_seconds() / 86400.0 if last_seen else None
    )

    # Карточка младше собственного окна: спрашивать не с чего.
    if hours_on_stage is not None and hours_on_stage < days * 24:
        return {
            "proven": True,
            "reason": GAP_OUT_OF_WINDOW,
            "window_days": days,
            "days_quiet": days_quiet,
        }

    # Ветка «комментарий» смотрит только на комментарии. Незакрытое дело
    # «позвонить клиенту» — это план брокера, а не работа с клиентом, и
    # засчитывать его как след нельзя.
    comments = [e for e in window if e.get("kind") == "comment"]
    if _has_call(window):
        reason = PROVEN_BY_CALL
    elif not comments:
        reason = GAP_NO_TRACE
    elif claims_messaged:
        # Утверждение «написал клиенту» засчитывается только со скриншотом.
        has_screenshot = any(e.get("has_files") for e in comments)
        reason = PROVEN_BY_SCREENSHOT if has_screenshot else GAP_CLAIMED_MESSAGE
    elif not comment_informative:
        reason = GAP_EMPTY_COMMENT
    else:
        reason = PROVEN_BY_COMMENT

    return {
        "proven": reason in PROVEN,
        "reason": reason,
        "window_days": days,
        "days_quiet": days_quiet,
    }
