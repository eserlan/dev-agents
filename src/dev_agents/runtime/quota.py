"""Provider usage-limit gate shared by every daemon that runs agents on one account.

When a provider reports that the account is over its usage limit, every run started
before the quoted reset time fails the same way. Recording the reset time lets the
fixers skip claiming work (no attempt is counted, nothing is posted) and keeps a
quota outage from auto-pausing PRs and issues that did nothing wrong.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# Codex: "You've hit your usage limit ... try again at 11:20 PM".
# Claude: "You've hit your limit · resets 11pm (Europe/Oslo)".
USAGE_LIMIT_PATTERN = re.compile(r"hit your (?:usage )?limit|usage limit reached", re.IGNORECASE)
_RESET_AT = re.compile(r"(?:try again at|resets) (\d{1,2})(?::(\d{2}))?\s*([AP]M)?", re.IGNORECASE)
# Used when the message names no reset time we can parse.
DEFAULT_BLOCK = timedelta(hours=1)


def quota_root() -> Path:
    return Path.home() / ".local/state/dev-agents/quota"


def usage_limit_reset(text: str, now: datetime) -> datetime | None:
    """Return when the usage limit in `text` lifts, or None if `text` reports no limit.

    The provider quotes a local wall-clock time; a time already past today means tomorrow.
    """
    if not USAGE_LIMIT_PATTERN.search(text):
        return None
    match = _RESET_AT.search(text)
    if match is None:
        return now + DEFAULT_BLOCK
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if meridiem:
        hour = hour % 12 + (12 if meridiem.upper() == "PM" else 0)
    if hour > 23 or minute > 59:
        return now + DEFAULT_BLOCK
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=1)


def record_quota_block(provider: str, until: datetime, root: Path | None = None) -> None:
    path = (root or quota_root()) / f"{provider}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"until": until.isoformat()}), encoding="utf-8")


def quota_blocked_until(
    provider: str, now: datetime | None = None, root: Path | None = None
) -> datetime | None:
    """Return the reset time while `provider` is over its usage limit, else None."""
    path = (root or quota_root()) / f"{provider}.json"
    try:
        until = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return until if until > (now or datetime.now().astimezone()) else None
