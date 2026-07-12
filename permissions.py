"""Permission helpers for Telegram group admins + shop admins."""

from __future__ import annotations

import logging
from typing import Any

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

import db
from config import OWNER_IDS

log = logging.getLogger("inventory_bot.permissions")


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
    """OWNER_IDS or Telegram group admin may run setup for a group."""
    if is_global_owner(user_id):
        return True
    return await is_group_admin(bot, chat_id, user_id)
