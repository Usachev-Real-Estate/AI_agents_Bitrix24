"""Bitrix24 personal notifications (im.notify.personal.add)."""

import asyncio
import logging
from typing import Any

from fast_bitrix24 import Bitrix

from config import BX_EXECUTOR, get_settings

logger = logging.getLogger(__name__)

# Жёсткий предел одного сообщения Bitrix24. Строка длиннее режется по нему,
# иначе хвост молча теряется на стороне портала.
MESSAGE_HARD_LIMIT = 4000


def _bx_call_sync(method: str, params: dict[str, Any]) -> Any:
    """Run Bitrix REST call in a thread-safe sync context.

    fast_bitrix24.Bitrix.call() returns a pending Task when called from a
    running asyncio loop; tools and scripts must use this helper instead.

    Args:
        method: REST API method name.
        params: Request parameters.

    Returns:
        API response (method-specific).
    """

    def _run() -> Any:
        # Прогрессбар берётся из tools.BX_VERBOSE, а не заводится свой:
        # расшифровки идут через этот вызов по одному звонку, и в тихом
        # прогоне они рисовали бы по строке на каждый.
        from tools import BX_VERBOSE  # noqa: PLC0415 — циклический импорт

        bx = Bitrix(get_settings().b24_webhook_url, verbose=BX_VERBOSE)
        return bx.call(method, params)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()

    return BX_EXECUTOR.submit(_run).result()


def send_chat_message(chat_id: int, message: str) -> int:
    """Send message to a Bitrix24 chat via im.message.add.

    Args:
        chat_id: Chat ID (e.g., 22358).
        message: Message text.

    Returns:
        Message ID from Bitrix24.
    """
    result = _bx_call_sync(
        "im.message.add",
        {"DIALOG_ID": f"chat{chat_id}", "MESSAGE": message},
    )
    msg_id = int(result) if result is not None else 0
    logger.info("Chat message sent: chat_id=%s msg_id=%s", chat_id, msg_id)
    return msg_id


def send_user_chat_message(
    user_id: int,
    message: str,
    *,
    system: bool = True,
    important: bool = False,
    webhook_url: str | None = None,
) -> int:
    """Send a message to a user's personal Bitrix24 chat.

    Args:
        user_id: Recipient user ID (DIALOG_ID without chat prefix).
        message: Message text (BB-codes supported).
        system: If True, send as SYSTEM message (more prominent).
        important: If True, mark as important (Bitrix «важное сообщение»).
        webhook_url: Optional webhook URL. Message is sent as that webhook's owner.
            If omitted, uses B24_WEBHOOK_URL.

    Returns:
        Message ID from Bitrix24.
    """
    params: dict[str, Any] = {
        "DIALOG_ID": str(user_id),
        "MESSAGE": message,
        "SYSTEM": "Y" if system else "N",
    }
    if important:
        # IMPORTANT — флаг мессенджера «важное», PARAMS.IS_IMPORTANT — тот же
        # флаг на самом сообщении. Неизвестные ключи REST игнорирует, поэтому
        # ставятся оба: какой из них сработает, зависит от версии портала.
        params["IMPORTANT"] = "Y"
        params["PARAMS"] = {"IS_IMPORTANT": "Y"}
    if webhook_url:
        import httpx

        url = webhook_url.rstrip("/") + "/im.message.add"
        response = httpx.post(url, json=params, timeout=60.0)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RuntimeError(
                f"im.message.add failed: {data.get('error')} "
                f"{data.get('error_description')}"
            )
        result = data.get("result")
    else:
        result = _bx_call_sync("im.message.add", params)

    msg_id = int(result) if result is not None else 0
    logger.info(
        "User chat message sent: user_id=%s msg_id=%s system=%s important=%s",
        user_id,
        msg_id,
        system,
        important,
    )
    return msg_id


def send_personal_notify(user_id: int, message: str) -> int:
    """Send a notification-center ping via im.notify.personal.add.

    Сообщение в чат и уведомление — разные каналы портала: чат человек
    может не открыть неделю, уведомление висит на колокольчике.

    Args:
        user_id: Recipient user ID.
        message: Notification text (BB-codes supported).

    Returns:
        Notification ID from Bitrix24, or 0 if skipped.
    """
    if user_id <= 0:
        logger.warning("Skip personal notify: invalid user_id=%s", user_id)
        return 0
    result = _bx_call_sync(
        "im.notify.personal.add",
        {"USER_ID": user_id, "MESSAGE": message},
    )
    notify_id = int(result) if result is not None else 0
    logger.info(
        "Personal notify sent: user_id=%s notify_id=%s",
        user_id,
        notify_id,
    )
    return notify_id


def _split_message(message: str, chunk_size: int) -> list[str]:
    """Split text into chunks of at most chunk_size, breaking on newlines.

    A single line longer than chunk_size is hard-split: previously it was
    emitted whole and truncated by Bitrix.
    """
    chunks: list[str] = []
    current = ""

    for line in message.split("\n"):
        while len(line) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:chunk_size])
            line = line[chunk_size:]
        if len(current) + len(line) + 1 > chunk_size:
            if current:
                chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line

    if current:
        chunks.append(current)
    return chunks


def send_chat_message_chunked(chat_id: int, message: str, chunk_size: int = 4000) -> int:
    """Send a long message to chat, splitting into chunks.

    Args:
        chat_id: Chat ID.
        message: Full message text.
        chunk_size: Max chars per chunk (default 4000).

    Returns:
        Number of chunks sent.
    """
    chunk_size = min(chunk_size, MESSAGE_HARD_LIMIT)
    if len(message) <= chunk_size:
        send_chat_message(chat_id, message)
        return 1

    chunks = _split_message(message, chunk_size)
    for chunk in chunks:
        send_chat_message(chat_id, chunk)

    logger.info("Chat message chunked: chat_id=%s chunks=%d", chat_id, len(chunks))
    return len(chunks)


def send_user_chat_message_chunked(
    user_id: int,
    message: str,
    chunk_size: int = 1800,
    *,
    system: bool = True,
    important: bool = False,
) -> int:
    """Send a long personal chat message, splitting into chunks."""
    if user_id <= 0:
        logger.warning("Skip personal message: invalid user_id=%s", user_id)
        return 0
    if len(message) <= chunk_size:
        send_user_chat_message(
            user_id, message, system=system, important=important,
        )
        return 1

    chunks = _split_message(message, chunk_size)
    for chunk in chunks:
        send_user_chat_message(
            user_id, chunk, system=system, important=important,
        )
    logger.info(
        "User chat message chunked: user_id=%s chunks=%d important=%s",
        user_id,
        len(chunks),
        important,
    )
    return len(chunks)
