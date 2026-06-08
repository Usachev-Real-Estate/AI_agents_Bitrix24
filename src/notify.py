"""Bitrix24 personal notifications (im.notify.personal.add)."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fast_bitrix24 import Bitrix

from config import get_settings

logger = logging.getLogger(__name__)

MESSAGE_MAX_LEN = 2000
_EXECUTOR = ThreadPoolExecutor(max_workers=1)


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
        bx = Bitrix(get_settings().b24_webhook_url)
        return bx.call(method, params)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()

    return _EXECUTOR.submit(_run).result()


def send_personal_notification(user_id: int, message: str) -> int:
    """Send a personal notification to a Bitrix24 user.

    Uses im.notify.personal.add (USER_ID + MESSAGE).

    Args:
        user_id: Recipient user ID.
        message: Notification text (truncated to 2000 chars).

    Returns:
        Notification ID from Bitrix24.

    Raises:
        Exception: On API errors.
    """
    msg = message
    if len(msg) > MESSAGE_MAX_LEN:
        msg = msg[:1990] + "...[обрезано]"

    result = _bx_call_sync(
        "im.notify.personal.add",
        {"USER_ID": user_id, "MESSAGE": msg},
    )
    notify_id = int(result) if result is not None else 0
    logger.info("Personal notification sent: user_id=%s notify_id=%s", user_id, notify_id)
    return notify_id


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


def send_chat_message_chunked(chat_id: int, message: str, chunk_size: int = 4000) -> int:
    """Send a long message to chat, splitting into chunks.

    Args:
        chat_id: Chat ID.
        message: Full message text.
        chunk_size: Max chars per chunk (default 4000).

    Returns:
        Number of chunks sent.
    """
    if len(message) <= chunk_size:
        send_chat_message(chat_id, message)
        return 1

    lines = message.split("\n")
    chunks: list[str] = []
    current = ""

    for line in lines:
        if len(current) + len(line) + 1 > chunk_size:
            if current:
                chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line

    if current:
        chunks.append(current)

    for chunk in chunks:
        send_chat_message(chat_id, chunk)

    logger.info("Chat message chunked: chat_id=%s chunks=%d", chat_id, len(chunks))
    return len(chunks)
