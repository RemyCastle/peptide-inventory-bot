"""Multi-token standby pool for warm failover after a bot ban/invalid token."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("inventory_bot.token_pool")

STATE_VERSION = 1


def parse_tokens(
    bot_tokens_csv: str = "",
    single_token: str = "",
) -> list[str]:
    """
    Build ordered token list.
    Prefer BOT_TOKENS (comma-separated); fall back to TELEGRAM_BOT_TOKEN.
    """
    tokens: list[str] = []
    for part in (bot_tokens_csv or "").split(","):
        t = part.strip()
        if t and t not in tokens:
            tokens.append(t)
    single = (single_token or "").strip()
    if single and single not in tokens:
        # Single token first if pool empty; if pool set, append only if missing
        if not tokens:
            tokens.append(single)
        # if BOT_TOKENS already has entries, ignore duplicate single
    return tokens


def state_path(base: Path) -> Path:
    return Path(base)


def load_state(path: Path) -> dict:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read token state %s: %s", path, exc)
    return {"version": STATE_VERSION, "active_index": 0, "dead_tokens": []}


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "active_index": int(state.get("active_index", 0)),
        "dead_tokens": list(state.get("dead_tokens") or []),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def clamp_index(index: int, n: int) -> int:
    if n <= 0:
        return 0
    return int(index) % n


def resolve_active_index(
    tokens: list[str],
    preferred_index: int = 0,
    state_file: Optional[Path] = None,
) -> int:
    """Pick starting token index from env preference and persisted state."""
    n = len(tokens)
    if n == 0:
        return 0
    idx = clamp_index(preferred_index, n)
    if state_file and state_file.is_file():
        st = load_state(state_file)
        saved = int(st.get("active_index", idx))
        idx = clamp_index(saved, n)
    return idx


def mark_token_dead(state_file: Path, token: str, tokens: list[str], current_index: int) -> int:
    """
    Record dead token fingerprint and advance to next live index.
    Returns new active index (or same if only one token).
    """
    st = load_state(state_file)
    dead = list(st.get("dead_tokens") or [])
    fp = token_fingerprint(token)
    if fp not in dead:
        dead.append(fp)
    n = len(tokens)
    if n == 0:
        next_idx = 0
    else:
        next_idx = clamp_index(current_index + 1, n)
        # Skip tokens already marked dead if possible
        for _ in range(n):
            if token_fingerprint(tokens[next_idx]) not in dead:
                break
            next_idx = clamp_index(next_idx + 1, n)
    st["dead_tokens"] = dead
    st["active_index"] = next_idx
    save_state(state_file, st)
    log.warning(
        "Marked token …%s dead; next active_index=%s",
        fp[-6:],
        next_idx,
    )
    return next_idx


def token_fingerprint(token: str) -> str:
    """Short non-secret id for logs/state (last segment / last 8 chars)."""
    t = (token or "").strip()
    if not t:
        return "empty"
    if ":" in t:
        return t.split(":", 1)[0] + ":" + t[-4:]
    return t[-8:]


def is_fatal_token_error(exc: BaseException) -> bool:
    """True if the bot token is invalid/revoked/banned and we should failover."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("InvalidToken", "Unauthorized"):
        return True
    # telegram.error.Conflict is another instance running — not a token death
    if name == "Conflict":
        return False
    fatal_snippets = (
        "unauthorized",
        "invalid token",
        "token is invalid",
        "bot was blocked",
        "bot was deleted",
        "forbidden: bot is not a member",  # not always fatal for whole bot
    )
    # Only treat clear auth failures as failover
    auth_snippets = (
        "unauthorized",
        "invalid token",
        "token is invalid",
        "not found",  # getMe style
    )
    if any(s in msg for s in auth_snippets) and "conflict" not in msg:
        # "not found" alone is too broad — require token context
        if "not found" in msg and "token" not in msg and "bot" not in msg:
            return False
        if "unauthorized" in msg or "invalid token" in msg or "token is invalid" in msg:
            return True
    # python-telegram-bot InvalidToken message
    if "the token" in msg and "invalid" in msg:
        return True
    return False


class TokenDeadError(RuntimeError):
    """Raised to exit the polling loop and try the next standby token."""


def env_tokens() -> list[str]:
    return parse_tokens(
        os.getenv("BOT_TOKENS", ""),
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
