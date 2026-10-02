"""Permission helpers for Telegram group admins + shop admins."""

from __future__ import annotations

import logging
from typing import Any

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

import db

log = logging.getLogger("inventory_bot.permissions")


def is_allowlisted_admin(user_id: int | None) -> bool:
    """True only when user_id is listed in ADMIN_TELEGRAM_IDS.

    An empty allowlist denies every sender. This is checked before shop-admin
    or owner rights, so a database admin row cannot unlock commands by itself.
    """
    try:
        uid = int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if uid <= 0:
        return False
    from config import ADMIN_TELEGRAM_IDS

    return uid in ADMIN_TELEGRAM_IDS


async def is_group_admin(bot: Any, chat_id: int, user_id: int) -> bool:
    """True if user is creator/administrator of the Telegram chat."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as exc:
        log.info("is_group_admin failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False
    except Exception as exc:  # pragma: no cover
        log.info("is_group_admin error chat=%s user=%s: %s", chat_id, user_id, exc)
        return False


def is_shop_admin(user_id: int, shop_id: int) -> bool:
    """DB shop admin or global owner."""
    return db.is_admin(shop_id, user_id)


def is_global_owner(user_id: int) -> bool:
    return db.is_owner(user_id)


async def can_setup_group_shop(bot: Any, chat_id: int, user_id: int) -> bool:
    """Allowlisted owner or allowlisted Telegram group admin may run setup."""
    if not is_allowlisted_admin(user_id):
        return False
    if is_global_owner(user_id):
        return True
    return await is_group_admin(bot, chat_id, user_id)
