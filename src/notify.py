"""Bitrix24 personal notifications (im.notify.personal.add)."""

import asyncio
import logging
from typing import Any

from fast_bitrix24 import Bitrix

from config import BX_EXECUTOR, get_settings

logger = logging.getLogger(__name__)

MESSAGE_MAX_LEN = 2000


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
    webhook_url: str | None = None,
) -> int:
    """Send a message to a user's personal Bitrix24 chat.

    Args:
        user_id: Recipient user ID (DIALOG_ID without chat prefix).
        message: Message text (BB-codes supported).
        system: If True, send as SYSTEM message (more prominent).
        webhook_url: Optional webhook URL. Message is sent as that webhook's owner.
            If omitted, uses B24_WEBHOOK_URL.

    Returns:
        Message ID from Bitrix24.
    """
    params = {
        "DIALOG_ID": str(user_id),
        "MESSAGE": message,
        "SYSTEM": "Y" if system else "N",
    }
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
        "User chat message sent: user_id=%s msg_id=%s system=%s",
        user_id,
        msg_id,
        system,
    )
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


def send_user_chat_message_chunked(
    user_id: int,
    message: str,
    chunk_size: int = 1800,
    *,
    system: bool = True,
) -> int:
    """Send a long personal chat message, splitting into chunks."""
    if user_id <= 0:
        logger.warning("Skip personal message: invalid user_id=%s", user_id)
        return 0
    if len(message) <= chunk_size:
        send_user_chat_message(user_id, message, system=system)
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
        send_user_chat_message(user_id, chunk, system=system)
    logger.info(
        "User chat message chunked: user_id=%s chunks=%d", user_id, len(chunks),
    )
    return len(chunks)
