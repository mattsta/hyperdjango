"""
Flash messages — server-side messages that survive a single redirect.

Stored in the session, displayed once, then cleared.

Usage:
    from hyperdjango.messages import add_message, get_messages, success, error, info, warning

    # In a view handler:
    async def create_product(request):
        ...
        success(request, "Product created successfully")
        return Response.redirect("/products/")

    # In the next view (after redirect):
    async def list_products(request):
        messages = get_messages(request)  # [{"level": "success", "text": "Product created successfully"}]
        # Messages are automatically cleared after retrieval

    # Convenience functions:
    success(request, "Done!")
    error(request, "Something went wrong")
    info(request, "FYI: ...")
    warning(request, "Watch out!")
"""

from typing import Any

from hyperdjango.conf import get_setting

# Message levels (numeric for threshold filtering)
DEBUG = 10
INFO = 20
SUCCESS = 25
WARNING = 30
ERROR = 40

# String tags for each level (used in templates)
LEVEL_TAGS: dict[int, str] = {
    DEBUG: "debug",
    INFO: "info",
    SUCCESS: "success",
    WARNING: "warning",
    ERROR: "error",
}

# Session key for storing messages
_SESSION_KEY = "_messages"


def add_message(request: Any, level: int, text: str):
    """Add a flash message to the request's session.

    The message will be available on the next request (typically after redirect)
    and automatically cleared after retrieval. Messages below MESSAGE_LEVEL
    threshold are silently discarded.
    """
    min_level = get_setting("MESSAGE_LEVEL")
    if level < min_level:
        return
    messages = _get_message_store(request)
    messages.append({"level": level, "text": text})
    _set_message_store(request, messages)


def get_level_tag(level: int) -> str:
    """Get the CSS class tag for a message level.

    Checks MESSAGE_TAGS setting first, then falls back to LEVEL_TAGS defaults.
    """
    custom_tags = get_setting("MESSAGE_TAGS")
    if custom_tags and level in custom_tags:
        return custom_tags[level]
    return LEVEL_TAGS.get(level, "")


def get_messages(request: Any, clear: bool = True) -> list[dict]:
    """Retrieve and optionally clear all flash messages.

    Returns a list of dicts: [{"level": 25, "text": "...", "tag": "success"}, ...]
    Messages are cleared by default after retrieval.
    """
    messages = _get_message_store(request)
    if clear and messages:
        _set_message_store(request, [])
    # Annotate each message with its CSS tag
    for msg in messages:
        msg["tag"] = get_level_tag(msg["level"])
    return messages


# Convenience functions


def success(request: Any, text: str):
    """Add a success flash message."""
    add_message(request, SUCCESS, text)


def error(request: Any, text: str):
    """Add an error flash message."""
    add_message(request, ERROR, text)


def info(request: Any, text: str):
    """Add an info flash message."""
    add_message(request, INFO, text)


def warning(request: Any, text: str):
    """Add a warning flash message."""
    add_message(request, WARNING, text)


# ---------------------------------------------------------------------------
# Storage helpers — uses request.session or a per-request dict fallback
# ---------------------------------------------------------------------------


def _get_message_store(request: Any) -> list[dict]:
    """Get the messages list from the request's session."""
    # Try session-based storage first
    # dynamic-attr: request is Any — flash messages support both hyperdjango Request and Django HttpRequest; a session attr may be absent, which selects the per-request fallback below
    session = getattr(request, "session", None)
    if session is not None:
        if hasattr(session, "get"):
            return session.get(_SESSION_KEY, [])
        # dynamic-attr: non-dict session store — the flash key is a runtime string attribute on an arbitrary session proxy
        return getattr(session, _SESSION_KEY, [])

    # Fallback: per-request storage (won't survive redirects without middleware)
    if not hasattr(request, "_flash_messages"):
        request._flash_messages = []
    return request._flash_messages


def _set_message_store(request: Any, messages: list[dict]):
    """Store messages in the request's session."""
    # dynamic-attr: request is Any — flash messages support both hyperdjango Request and Django HttpRequest; a session attr may be absent, which selects the per-request fallback below
    session = getattr(request, "session", None)
    if session is not None:
        if hasattr(session, "__setitem__"):
            session[_SESSION_KEY] = messages
        else:
            # dynamic-attr: non-dict session store — the flash key is a runtime string attribute on an arbitrary session proxy
            setattr(session, _SESSION_KEY, messages)
        return

    request._flash_messages = messages


class MessageMiddleware:
    """Middleware that loads flash messages into the request context.

    Adds `request.messages` containing the list of pending messages.
    Messages are cleared after each request that reads them.

    Usage in middleware stack:
        app.add_middleware(MessageMiddleware)

    Then in templates:
        {% for msg in messages %}
            <div class="alert alert-{{ msg.level }}">{{ msg.text }}</div>
        {% endfor %}
    """

    async def __call__(self, request, handler):
        # Load messages before the handler runs
        request._pending_messages = get_messages(request, clear=True)
        response = await handler(request)
        return response
