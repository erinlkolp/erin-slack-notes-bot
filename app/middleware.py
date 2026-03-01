import os
import time
import functools
import logging
from collections import defaultdict

from .config import RATE_LIMIT_SECONDS, RATE_LIMIT_MAX_ENTRIES

logger = logging.getLogger(__name__)

_last_command_time = defaultdict(float)

# Read at import time; tests can patch this attribute directly.
allowed_user_id = os.environ.get("ALLOWED_SLACK_USER_ID")


def check_rate_limit(user_id, command_name):
    """Return True if the user is rate-limited for this command, False if allowed."""
    key = f"{user_id}:{command_name}"
    now = time.monotonic()
    last = _last_command_time[key]
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_command_time[key] = now
    # Evict stale entries to prevent unbounded memory growth.
    if len(_last_command_time) > RATE_LIMIT_MAX_ENTRIES:
        stale = [k for k, v in _last_command_time.items() if now - v > RATE_LIMIT_SECONDS]
        for k in stale:
            del _last_command_time[k]
    return False


def require_allowed_user(command_name=None, is_view=False):
    """Decorator that enforces single-user authorization and optional rate limiting.

    Works for slash-command handlers (ack/respond/command), action handlers
    (ack/respond/body), and modal view handlers (ack/body/view).

    Args:
        command_name: If provided, rate-limiting is applied using this key.
        is_view: Set True for @app.view handlers.  The decorator will NOT
                 pre-ack so the handler can return its own response_action
                 (e.g. validation errors).  Unauthorized submissions are
                 silently dismissed via ack().
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ack = kwargs.get("ack")
            respond = kwargs.get("respond")
            command = kwargs.get("command")
            body = kwargs.get("body")

            if command:
                user_id = command.get("user_id")
            elif body:
                user_id = body.get("user", {}).get("id")
            else:
                user_id = None

            if not is_view and ack:
                ack()

            if user_id != allowed_user_id:
                if is_view and ack:
                    ack()  # dismiss the modal silently for unauthorized submissions
                elif respond:
                    respond("🚫 Sorry, this bot is restricted to a specific user.")
                return

            if command_name and check_rate_limit(user_id, command_name):
                if respond:
                    respond("⏳ Please wait a few seconds before sending another command.")
                return

            return fn(*args, **kwargs)
        return wrapper
    return decorator
