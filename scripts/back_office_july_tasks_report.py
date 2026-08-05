#!/usr/bin/env python3
"""One-off report: Back-Office closed tasks in July — with/without closing message."""

from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import requests

from config import get_settings  # noqa: E402
from fast_bitrix24 import Bitrix  # noqa: E402

# July 2026 (current month per request date)
JULY_START = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
JULY_END = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

COMPLETED_STATUSES = {5, "5", "completed"}
SERVICE_COMMENT_MARKERS = (
    "задача завершена",
    "задача выполнена",
    "задача закрыта",
    "завершил задачу",
    "завершила задачу",
    "завершили задачу",
    "статус изменён",
    "статус изменен",
    "status changed",
    "task completed",
    "task closed",
    "задача просрочена",
    "задача почти просрочена",
    "создал задачу",
    "создала задачу",
    "создали задачу",
    "начал выполнять",
    "начала выполнять",
)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _task_id(task: dict) -> int | None:
    raw = task.get("id") or task.get("ID")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_tasks(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tasks", "items", "result"):
        val = payload.get(key)
        if isinstance(val, list):
            return [t for t in val if isinstance(t, dict)]
        if isinstance(val, dict):
            return [t for t in val.values() if isinstance(t, dict)]
    return []


def _user_name(user: dict) -> str:
    return f"{user.get('NAME', '')} {user.get('LAST_NAME', '')}".strip()


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _is_meaningful_message(text: str) -> bool:
    cleaned = _strip_html(text)
    cleaned = re.sub(r"\[USER=\d+][^\[]*\[/USER\]", "", cleaned).strip()
    if len(cleaned) <= 3:
        return False
    low = cleaned.lower()
    if low.startswith("[url=") or "timestamps=" in low:
        return False
    return not any(marker in low for marker in SERVICE_COMMENT_MARKERS)


def _extract_accomplices(task: dict) -> set[int]:
    raw = task.get("accomplices") or task.get("ACCOMPLICES") or []
    ids: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                uid = item.get("id") or item.get("ID")
            else:
                uid = item
            try:
                ids.add(int(uid))
            except (TypeError, ValueError):
                pass
    return ids


async def fetch_back_office_users(client: B24Client, dept_id: int) -> list[dict]:
    users = await client.get_all("user.get", {
        "FILTER": {"UF_DEPARTMENT": dept_id, "ACTIVE": True},
        "SELECT": ["ID", "NAME", "LAST_NAME", "UF_DEPARTMENT"],
    })
    return users if isinstance(users, list) else []


async def fetch_closed_tasks_for_user(client: B24Client, user_id: int) -> list[dict]:
    """Tasks where user is responsible or accomplice, closed in July."""
    select = [
        "ID", "TITLE", "STATUS", "REAL_STATUS", "RESPONSIBLE_ID",
        "CLOSED_DATE", "CLOSED_BY", "ACCOMPLICES", "CREATED_DATE", "CHAT_ID",
    ]
    seen: dict[int, dict] = {}

    for role_filter in (
        {"RESPONSIBLE_ID": user_id},
        {"ACCOMPLICE": user_id},
    ):
        payload = await client.get_all("tasks.task.list", {
            "filter": {
                **role_filter,
                ">=CLOSED_DATE": JULY_START.isoformat(),
                "<CLOSED_DATE": JULY_END.isoformat(),
                "REAL_STATUS": 5,
            },
            "select": select,
        })
        for task in _normalize_tasks(payload):
            tid = _task_id(task)
            if tid is not None:
                seen[tid] = task

    return list(seen.values())


class B24Client:
    """Minimal REST client (raw requests for chat messages)."""

    def __init__(self, webhook_url: str) -> None:
        self._base = webhook_url.rstrip("/") + "/"
        self._bx = Bitrix(webhook_url)

    @property
    def bx(self) -> Bitrix:
        return self._bx

    def call(self, method: str, params: dict) -> Any:
        response = requests.post(self._base + method, json=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"{method}: {payload['error']} — {payload.get('error_description')}")
        return payload.get("result", payload)

    async def get_all(self, method: str, params: dict) -> Any:
        return await self._bx.get_all(method, params)


def _fetch_chat_messages(client: B24Client, chat_id: int) -> list[dict]:
    messages: list[dict] = []
    last_id: int | None = None
    while True:
        params: dict[str, Any] = {"DIALOG_ID": f"chat{chat_id}", "LIMIT": 50}
        if last_id is not None:
            params["LAST_ID"] = last_id
        result = client.call("im.dialog.messages.get", params)
        batch = result.get("messages", []) if isinstance(result, dict) else []
        if not batch:
            break
        messages.extend(batch)
        oldest = min(int(m["id"]) for m in batch if m.get("id") is not None)
        if last_id is not None and oldest >= last_id:
            break
        last_id = oldest
        if len(batch) < 50:
            break
    return messages


def _closing_message_from_chat(
    messages: list[dict],
    closed_at: datetime | None,
) -> str | None:
    """Find user comment left when completing the task (chat feed)."""
    if not messages:
        return None

    parsed: list[tuple[datetime, int, str]] = []
    for msg in messages:
        text = _strip_html(str(msg.get("text") or ""))
        if not text:
            continue
        post_dt = _parse_dt(msg.get("date"))
        if post_dt is None:
            continue
        try:
            author_id = int(msg.get("author_id") or 0)
        except (TypeError, ValueError):
            author_id = 0
        parsed.append((post_dt, author_id, text))

    if not parsed:
        return None

    parsed.sort(key=lambda x: x[0])

    close_events = [
        (dt, text)
        for dt, author_id, text in parsed
        if author_id == 0 and any(m in text.lower() for m in ("завершил задачу", "завершила задачу"))
    ]
    if close_events and closed_at is not None:
        close_dt = min(close_events, key=lambda x: abs((x[0] - closed_at).total_seconds()))[0]
    elif close_events:
        close_dt = close_events[-1][0]
    elif closed_at is not None:
        close_dt = closed_at
    else:
        close_dt = parsed[-1][0]

    candidates: list[tuple[datetime, str]] = []
    for post_dt, author_id, text in parsed:
        if author_id <= 0:
            continue
        if not _is_meaningful_message(text):
            continue
        if post_dt > close_dt:
            continue
        if (close_dt - post_dt).total_seconds() > 300:  # 5 min before close
            continue
        candidates.append((post_dt, text))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


async def analyze_user_tasks(client: B24Client, user: dict) -> dict[str, Any]:
    user_id = int(user["ID"])
    name = _user_name(user)
    tasks = await fetch_closed_tasks_for_user(client, user_id)

    with_message: list[dict] = []
    without_message: list[dict] = []

    for task in tasks:
        tid = _task_id(task)
        title = task.get("title") or task.get("TITLE") or "—"
        closed_at = _parse_dt(task.get("closedDate") or task.get("CLOSED_DATE"))
        closed_by_raw = task.get("closedBy") or task.get("CLOSED_BY")
        try:
            closed_by = int(closed_by_raw) if closed_by_raw is not None else None
        except (TypeError, ValueError):
            closed_by = None

        responsible_raw = task.get("responsibleId") or task.get("RESPONSIBLE_ID")
        try:
            responsible_id = int(responsible_raw) if responsible_raw is not None else None
        except (TypeError, ValueError):
            responsible_id = None

        accomplices = _extract_accomplices(task)
        if user_id == responsible_id:
            role = "исполнитель"
        elif user_id in accomplices:
            role = "соисполнитель"
        else:
            role = "участник"

        chat_raw = task.get("chatId") or task.get("CHAT_ID")
        try:
            chat_id = int(chat_raw) if chat_raw is not None else None
        except (TypeError, ValueError):
            chat_id = None

        if chat_id is None and tid is not None:
            try:
                detail = client.call("tasks.task.get", {"taskId": tid, "select": ["CHAT_ID"]})
                if isinstance(detail, dict):
                    chat_raw = detail.get("chatId") or detail.get("CHAT_ID")
                    chat_id = int(chat_raw) if chat_raw is not None else None
            except Exception:
                chat_id = None

        messages = _fetch_chat_messages(client, chat_id) if chat_id else []
        closing_msg = _closing_message_from_chat(messages, closed_at)

        entry = {
            "id": tid,
            "title": title,
            "role": role,
            "closed_at": closed_at.strftime("%d.%m.%Y %H:%M") if closed_at else "?",
            "closed_by": closed_by,
            "message": closing_msg,
        }
        if closing_msg:
            with_message.append(entry)
        else:
            without_message.append(entry)

    return {
        "user_id": user_id,
        "name": name,
        "with_message": with_message,
        "without_message": without_message,
        "total": len(with_message) + len(without_message),
    }


def format_report(results: list[dict]) -> str:
    lines = [
        "Отчёт: БЭК-ОФИС — закрытые задачи за июль 2026",
        f"Период: 01.07.2026 — 31.07.2026",
        "",
    ]

    total_all = sum(r["total"] for r in results)
    total_with = sum(len(r["with_message"]) for r in results)
    total_without = sum(len(r["without_message"]) for r in results)

    for r in sorted(results, key=lambda x: x["name"]):
        if r["total"] == 0:
            lines.extend([
                f"👤 {r['name']} — закрытых задач за июль: 0",
                "",
            ])
            continue

        lines.extend([
            "═" * 50,
            f"👤 {r['name']} — закрыто: {r['total']} "
            f"(✅ с сообщением: {len(r['with_message'])}, "
            f"❌ без сообщения: {len(r['without_message'])})",
            "═" * 50,
        ])

        if r["with_message"]:
            lines.append("")
            lines.append("✅ Закрыты С сообщением:")
            for i, t in enumerate(r["with_message"], 1):
                lines.append(
                    f"   {i}. #{t['id']} [{t['role']}] «{t['title']}» "
                    f"({t['closed_at']})"
                )
                lines.append(f"      💬 {t['message'][:200]}")

        if r["without_message"]:
            lines.append("")
            lines.append("❌ Закрыты БЕЗ сообщения:")
            for i, t in enumerate(r["without_message"], 1):
                lines.append(
                    f"   {i}. #{t['id']} [{t['role']}] «{t['title']}» "
                    f"({t['closed_at']})"
                )
        lines.append("")

    lines.extend([
        "═" * 50,
        "📊 СВОДКА ПО ОТДЕЛУ",
        "═" * 50,
        f"Сотрудников: {len(results)}",
        f"Всего закрытых задач (с учётом роли): {total_all}",
        f"С сообщением при закрытии: {total_with}",
        f"Без сообщения при закрытии: {total_without}",
        f"Доля с сообщением: {int(total_with / total_all * 100) if total_all else 0}%",
    ])
    return "\n".join(lines)


async def async_main() -> None:
    settings = get_settings()
    if not settings.back_office_dept_id:
        raise SystemExit("BACK_OFFICE_DEPT_ID не задан в .env")

    client = B24Client(settings.b24_webhook_url)
    users = await fetch_back_office_users(client, settings.back_office_dept_id)
    print(f"Найдено сотрудников БЭК-Офис: {len(users)}")

    results = []
    for user in users:
        name = _user_name(user)
        print(f"  Обработка: {name}...", flush=True)
        results.append(await analyze_user_tasks(client, user))

    report = format_report(results)
    print("\n" + report)

    out_path = Path(__file__).resolve().parent.parent / "data" / "back_office_july_2026_tasks_report.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\nОтчёт сохранён: {out_path}")


if __name__ == "__main__":
    asyncio.run(async_main())
