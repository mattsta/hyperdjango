"""
HyperNews — a HackerNews/Reddit clone built with HyperDjango.

Demonstrates:
- HyperApp with native Zig server, templates, static files
- Model definitions with SlugField, foreign keys
- Session-based auth with argon2 password hashing
- HTMX-powered voting, commenting, reply forms
- HyperAdmin with custom actions (ban, mute, delete spam)
- CSRF protection, rate limiting, security headers
- Threaded comments with depth tracking
- Karma system via signals
- Background spam detection task
"""

import asyncio
import atexit
import html as _html
import re
import sys as _sys
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.admin.fields import Action
from hyperdjango.auth import hash_password, verify_password
from hyperdjango.auth.sessions import (
    SessionAuth,
    build_session_data,
    is_safe_redirect_url,
)
from hyperdjango.cache import LocMemCache
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.db.pgzig_connection import IntegrityError
from hyperdjango.expressions import Exists, F, OuterRef
from hyperdjango.guard import Require, guard
from hyperdjango.humanize import time_bucket_cached
from hyperdjango.logging import logger
from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.rest import _decode_cursor, _encode_cursor
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
    VersionMiddleware,
)
from hyperdjango.telemetry import configure_from_settings
from hyperdjango.timeline import (
    EscalationEngine,
    EscalationRule,
    StatusEvent,
    get_timeline,
    register_timeline_admin,
)
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors

from .config import load_hypernews_config
from .models import (
    AdminMessage,
    AutomodAction,
    AutomodRule,
    AutomodTrigger,
    Award,
    AwardType,
    Bookmark,
    Comment,
    Forum,
    ForumMember,
    ForumRole,
    Notification,
    NotificationType,
    Poll,
    PollOption,
    PollType,
    PollVote,
    Post,
    PostRevision,
    PostStatus,
    Sequence,
    SequenceEntry,
    SpamReport,
    User,
    UserFlair,
    UserProfile,
    Vote,
)
from .voting import (
    AgreementVote,
    ModAction,
    ModNote,
    TargetType,
    Visibility,
    apply_content_tag,
    check_action_rate_limit,
    check_downvote_permission,
    check_self_vote,
    check_vote_rate_limit,
    cleanup_old_data,
    detect_voting_rings,
    ensure_voting_tables,
    get_vote_weight,
    log_rapid_fire,
    query_centrality_leaders,
    query_communities,
    query_domain_authority,
    query_user_affinity,
    record_vote_event,
    refresh_hot_scores,
    run_graph_analytics,
    run_ring_detection,
)

# ---------------------------------------------------------------------------
# Validated input schemas (Pydantic v2-compatible BaseModel)
# ---------------------------------------------------------------------------


class AutomodConditionSchema(ValidatedModel):
    """Schema for automod rule conditions. Auto-validates types and rejects unknown keys."""

    model_config = {"extra": "forbid"}

    min_karma: int | None = None
    contains_words: list[str] | None = None
    link_count_gt: int | None = None


class CreateForumSchema(ValidatedModel):
    """POST /forums/create — forum name, title, description, rules."""

    name: str = VField(
        min_length=1,
        max_length=30,
        pattern=r"^[a-z0-9_-]+$",
        strip_whitespace=True,
        to_lower=True,
    )
    title: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    description: str = VField(default="", max_length=2000, strip_whitespace=True)
    rules: str = VField(default="", max_length=5000, strip_whitespace=True)


class ForumEditSchema(ValidatedModel):
    """POST /f/{name}/edit — forum settings update."""

    title: str = VField(min_length=1, max_length=100, strip_whitespace=True)
    description: str = VField(default="", max_length=2000, strip_whitespace=True)
    rules: str = VField(default="", max_length=5000, strip_whitespace=True)
    is_public: str = VField(default="")  # "on" or empty from checkbox
    is_archived: str = VField(default="")
    is_locked: str = VField(default="")
    is_hidden: str = VField(default="")


class SubmitPostSchema(ValidatedModel):
    """POST /submit — global post submission with optional forum."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    url: str = VField(default="", max_length=2000, strip_whitespace=True)
    text: str = VField(default="", max_length=10000, strip_whitespace=True)
    forum: str = VField(default="", strip_whitespace=True)


class ForumSubmitPostSchema(ValidatedModel):
    """POST /f/{name}/submit — post submission within a forum."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    url: str = VField(default="", max_length=2000, strip_whitespace=True)
    text: str = VField(default="", max_length=10000, strip_whitespace=True)


class RegisterSchema(ValidatedModel):
    """POST /register — user registration."""

    username: str = VField(
        min_length=1, max_length=30, pattern=r"^[a-zA-Z0-9_-]+$", strip_whitespace=True
    )
    password: str = VField(min_length=8)
    email: str = VField(default="", max_length=254, strip_whitespace=True)


class LoginSchema(ValidatedModel):
    """POST /login — user authentication."""

    username: str = VField(min_length=1, strip_whitespace=True)
    password: str = VField(min_length=1)


class CommentSchema(ValidatedModel):
    """POST /comment — add comment to a post."""

    post_id: int = VField(ge=1)
    parent_id: int = VField(default=0, ge=0)
    text: str = VField(min_length=1, max_length=5000, strip_whitespace=True)


class VoteSchema(ValidatedModel):
    """POST /vote — upvote/downvote."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    direction: str = VField(default="up", pattern=r"^(up|down)$")


class BookmarkSchema(ValidatedModel):
    """POST /bookmark — toggle bookmark."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)


class ReportSchema(ValidatedModel):
    """POST /report — report spam/inappropriate content."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    reason: str = VField(default="", max_length=500, strip_whitespace=True)


class EditPostSchema(ValidatedModel):
    """POST /post/{pid}/edit — edit a published post."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    text: str = VField(default="", max_length=10000, strip_whitespace=True)
    edit_reason: str = VField(default="", max_length=200, strip_whitespace=True)


class DraftSchema(ValidatedModel):
    """POST /draft — save a post as draft."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    text: str = VField(default="", max_length=10000, strip_whitespace=True)
    forum: str = VField(default="", strip_whitespace=True)


class PollCreateSchema(ValidatedModel):
    """POST /post/{pid}/poll/create — attach a poll to a post."""

    question: str = VField(min_length=1, max_length=500, strip_whitespace=True)
    poll_type: str = VField(
        default="single_choice", pattern=r"^(single_choice|multiple_choice)$"
    )
    options: str = VField(min_length=1, strip_whitespace=True)  # newline-separated


class CrosspostSchema(ValidatedModel):
    """POST /post/{pid}/crosspost — crosspost to another forum."""

    forum: str = VField(min_length=1, strip_whitespace=True)


class FlairSchema(ValidatedModel):
    """POST /f/{name}/flair — set user flair."""

    username: str = VField(default="", max_length=30, strip_whitespace=True)
    flair_text: str = VField(default="", max_length=50, strip_whitespace=True)
    css_class: str = VField(default="custom", max_length=20, strip_whitespace=True)


class AwardSchema(ValidatedModel):
    """POST /award — give an award to a post or comment."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    award_type: str = VField(
        default="insightful", pattern=r"^(insightful|well_written|helpful|funny)$"
    )


class SequenceCreateSchema(ValidatedModel):
    """POST /sequences — create a new sequence."""

    title: str = VField(min_length=1, max_length=300, strip_whitespace=True)
    description: str = VField(default="", max_length=2000, strip_whitespace=True)


class AutomodCreateSchema(ValidatedModel):
    """POST /f/{name}/automod — create an automod rule."""

    trigger: str = VField(pattern=r"^(new_post|new_comment|report_threshold)$")
    action: str = VField(pattern=r"^(remove|flag|notify_mods)$")
    condition: str = VField(default="{}")


class AccountSchema(ValidatedModel):
    """POST /account — update account settings."""

    display_name: str = VField(default="", max_length=60, strip_whitespace=True)
    bio: str = VField(default="", max_length=500, strip_whitespace=True)
    email: str = VField(default="", max_length=254, strip_whitespace=True)
    current_password: str = VField(default="")
    new_password: str = VField(default="")


class ProfileSettingsSchema(ValidatedModel):
    """POST /settings/profile — update extended profile."""

    display_name: str = VField(default="", max_length=60, strip_whitespace=True)
    bio: str = VField(default="", max_length=500, strip_whitespace=True)
    email: str = VField(default="", max_length=254, strip_whitespace=True)
    website: str = VField(default="", max_length=500, strip_whitespace=True)
    location: str = VField(default="", max_length=100, strip_whitespace=True)
    avatar_url: str = VField(default="", max_length=500, strip_whitespace=True)
    github_username: str = VField(default="", max_length=40, strip_whitespace=True)


class ModAppointSchema(ValidatedModel):
    """POST /f/{name}/mod/appoint, /mod/remove, /mod/transfer — username target."""

    username: str = VField(min_length=1, max_length=30, strip_whitespace=True)


class ModNoteSchema(ValidatedModel):
    """POST /mod/note — add a moderator note."""

    target_user_id: int = VField(default=0, ge=0)
    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    note: str = VField(min_length=1, max_length=2000, strip_whitespace=True)
    visibility: str = VField(default="mod_only")


class AgreeVoteSchema(ValidatedModel):
    """POST /agree — agree/disagree meta-vote."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    direction: str = VField(default="agree", pattern=r"^(agree|disagree)$")


class TagSchema(ValidatedModel):
    """POST /tag — apply content tag."""

    post_id: int = VField(default=0, ge=0)
    comment_id: int = VField(default=0, ge=0)
    tag: str = VField(min_length=1, strip_whitespace=True)


class PollVoteSchema(ValidatedModel):
    """POST /poll/{poll_id}/vote — vote on a poll option."""

    option_id: int = VField(ge=1)


class SequenceAddSchema(ValidatedModel):
    """POST /sequence/{seq_id}/add and /remove — post ID for sequence ops."""

    pid: str = VField(min_length=1, strip_whitespace=True)


class MessageSchema(ValidatedModel):
    """POST /messages/send — send a direct message."""

    to_username: str = VField(min_length=1, strip_whitespace=True)
    subject: str = VField(min_length=1, max_length=200, strip_whitespace=True)
    body: str = VField(min_length=1, max_length=2000, strip_whitespace=True)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent

_DEBUG = get_setting("DEBUG")

_site_config = load_hypernews_config()

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hypernews"
)

app = HyperApp(
    database=get_setting("DATABASE_URL"),
    templates=str(_APP_DIR / "templates"),
    static=str(_APP_DIR / "static"),
    debug=_DEBUG,
    secret_key=get_setting("SECRET_KEY"),
    site_config=_site_config,
)

# --- Native telemetry (v0.15.1) -----------------------------------------------
# Auto-enables in debug mode. In production, set HYPER_TELEMETRY_ENABLED=1.
if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0
_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:
    app.get("/metrics")(_telemetry.prometheus_sink.handler)

# Middleware stack (outermost first)
app.use(VersionMiddleware())
app.use(TimingMiddleware())
app.use(
    SecurityHeadersMiddleware(
        hsts=not _DEBUG,  # Enable HSTS in production
        csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
    )
)
csrf = CSRFMiddleware(
    secret=get_setting("CSRF_SECRET"),
    exempt_paths=set(),
    exempt_prefixes={"/admin/"},  # HyperAdmin has its own CSRF protection
)
app.use(csrf)
_rate_limit = get_setting("RATE_LIMIT_REQUESTS")
if _rate_limit > 0:
    app.use(RateLimitMiddleware(max_requests=_rate_limit, window=60))

# Session auth
_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_session_engine,
)
app.use(auth)


@app.exception_handler(Exception)
async def _handle_error(request, exc):
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Startup — ensure voting infrastructure tables exist
# ---------------------------------------------------------------------------


# ── Escalation Engine ─────────────────────────────────────────────────────
# Auto-applies consequences when moderation events accumulate.
# Wired to the status_changed signal — fires after each set_status().

_escalation = EscalationEngine()
_esc = _site_config.escalation

# N warnings → auto-mute (configurable via EscalationConfig)
_escalation.add_rule(
    "user",
    EscalationRule(
        trigger_status="warned",
        threshold=_esc.warn_to_mute_threshold,
        window=timedelta(days=_esc.warn_to_mute_window_days),
        consequence_category="moderation",
        consequence_status="muted",
        consequence_expires_in=timedelta(days=_esc.mute_duration_days),
        consequence_reason=f"Auto-muted: {_esc.warn_to_mute_threshold} warnings accumulated",
    ),
)

# N mutes within window → auto-ban (configurable via EscalationConfig)
_escalation.add_rule(
    "user",
    EscalationRule(
        trigger_status="muted",
        threshold=_esc.mute_to_ban_threshold,
        window=timedelta(days=_esc.mute_to_ban_window_days),
        consequence_category="moderation",
        consequence_status="banned",
        consequence_reason=f"Auto-banned: {_esc.mute_to_ban_threshold} mutes within {_esc.mute_to_ban_window_days} days",
    ),
)


@app.on_startup
async def _startup():
    db = get_db()
    await get_timeline().ensure_indexes()
    _escalation.connect()
    await ensure_voting_tables(db)
    await refresh_hot_scores(db)
    # Start periodic background refresh (every 60s)
    _start_hot_score_refresh()


@app.on_shutdown
async def _shutdown():
    _escalation.disconnect()


async def _reconcile_forum_counts_full(db) -> dict[str, int]:
    """Full reconciliation — returns count of forums fixed per counter type."""
    subs_rows = await db.query_tuples(
        """WITH actual AS (
            SELECT f.id, COUNT(fm.id) AS real_count
            FROM hn_forums f LEFT JOIN hn_forum_members fm ON fm.forum_id = f.id
            GROUP BY f.id
        )
        UPDATE hn_forums SET subscriber_count = actual.real_count
        FROM actual WHERE hn_forums.id = actual.id
          AND hn_forums.subscriber_count != actual.real_count
        RETURNING hn_forums.id"""
    )
    posts_rows = await db.query_tuples(
        """WITH actual AS (
            SELECT f.id, COUNT(p.id) AS real_count
            FROM hn_forums f LEFT JOIN hn_posts p ON p.forum_id = f.id AND NOT p.is_deleted
            GROUP BY f.id
        )
        UPDATE hn_forums SET post_count = actual.real_count
        FROM actual WHERE hn_forums.id = actual.id
          AND hn_forums.post_count != actual.real_count
        RETURNING hn_forums.id"""
    )
    return {"subscribers_fixed": len(subs_rows), "posts_fixed": len(posts_rows)}


_shutdown_event = threading.Event()


def _start_hot_score_refresh() -> None:
    """Start background thread that refreshes hot_score every 60s.

    Uses _shutdown_event for clean exit — avoids interpreter shutdown crash
    caused by daemon threads holding stdout/stderr locks.
    """

    def _refresh_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        cycle = 0
        while not _shutdown_event.wait(timeout=60):
            cycle += 1
            db = get_db()
            try:
                loop.run_until_complete(refresh_hot_scores(db))
                loop.run_until_complete(cleanup_old_data(db))
                if cycle % 10 == 0:
                    loop.run_until_complete(run_ring_detection(db))
                    loop.run_until_complete(_reconcile_forum_counts_full(db))
                if cycle % 1440 == 0:
                    loop.run_until_complete(run_graph_analytics(db))
            except Exception as exc:
                logger.error("Background refresh error: {err}", err=str(exc))
        loop.close()

    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    atexit.register(_shutdown_event.set)


# ---------------------------------------------------------------------------
# Cache — front page post lists cached for 10s, invalidated on new posts/votes
# ---------------------------------------------------------------------------

_cache = LocMemCache(max_size=256)

# ---------------------------------------------------------------------------
# Admin setup
# ---------------------------------------------------------------------------

admin = HyperAdmin(app, prefix="/admin", title=f"{_site_config.name} Admin")


async def ban_user_action(admin_instance, model_config, selected_ids, request):
    """Ban selected users via timeline (records who, when, and why)."""
    mod_id = get_uid(request)
    for uid in selected_ids:
        user = await User.objects.filter(id=int(uid)).first()
        if user:
            await user.set_status(
                "moderation", "banned", reason="Admin action", actor_id=mod_id
            )
    return f"Banned {len(selected_ids)} user(s)"


async def mute_user_action(admin_instance, model_config, selected_ids, request):
    """Mute selected users via timeline."""
    mod_id = get_uid(request)
    for uid in selected_ids:
        user = await User.objects.filter(id=int(uid)).first()
        if user:
            await user.set_status(
                "moderation", "muted", reason="Admin action", actor_id=mod_id
            )
    return f"Muted {len(selected_ids)} user(s)"


async def warn_user_action(admin_instance, model_config, selected_ids, request):
    """Warn selected users via timeline. Escalation rules auto-apply consequences."""
    mod_id = get_uid(request)
    for uid in selected_ids:
        user = await User.objects.filter(id=int(uid)).first()
        if user:
            await user.set_status(
                "moderation", "warned", reason="Admin warning", actor_id=mod_id
            )
    return f"Warned {len(selected_ids)} user(s)"


async def unban_user_action(admin_instance, model_config, selected_ids, request):
    """Unban selected users via timeline (clears moderation status)."""
    mod_id = get_uid(request)
    for uid in selected_ids:
        user = await User.objects.filter(id=int(uid)).first()
        if user:
            await user.clear_status("moderation", reason="Admin unban", actor_id=mod_id)
    return f"Unbanned {len(selected_ids)} user(s)"


async def mark_spam_reviewed(admin_instance, model_config, selected_ids, request):
    """Mark spam reports as reviewed."""
    for sid in selected_ids:
        await SpamReport.objects.filter(id=int(sid)).update(status="reviewed")
    return f"Marked {len(selected_ids)} report(s) as reviewed"


async def delete_spam_posts(admin_instance, model_config, selected_ids, request):
    """Delete posts flagged as spam. Decrements forum post_count atomically."""
    db = get_db()
    for sid in selected_ids:
        report = await SpamReport.objects.filter(id=int(sid)).first()
        if report and report.post_id:
            post = await Post.objects.filter(
                id=report.post_id, is_deleted=False
            ).first()
            if post:
                async with db.transaction():
                    await Post.objects.filter(id=post.id).update(is_deleted=True)
                    if post.forum_id:
                        await Forum.objects.filter(id=post.forum_id).update(
                            post_count=F("post_count") - 1
                        )
        await SpamReport.objects.filter(id=int(sid)).update(status="deleted")
    return f"Deleted {len(selected_ids)} spam post(s)"


admin.register(
    User,
    list_display=[
        "id",
        "username",
        "email",
        "karma",
    ],
    search_fields=["username", "email"],
    readonly_fields=["id", "password_hash", "created_at"],
    actions=[
        Action(name="warn_users", label="Warn selected", handler=warn_user_action),
        Action(name="ban_users", label="Ban selected", handler=ban_user_action),
        Action(name="mute_users", label="Mute selected", handler=mute_user_action),
        Action(name="unban_users", label="Unban selected", handler=unban_user_action),
    ],
)

admin.register(
    Post,
    list_display=[
        "id",
        "title",
        "author_id",
        "score",
        "comment_count",
        "is_deleted",
        "created_at",
    ],
    search_fields=["title"],
    list_filter=["is_ask", "is_show", "is_deleted"],
    readonly_fields=["id", "slug", "score", "comment_count", "created_at"],
)

admin.register(
    Comment,
    list_display=[
        "id",
        "post_id",
        "author_id",
        "depth",
        "score",
        "is_deleted",
        "created_at",
    ],
    search_fields=["text"],
    list_filter=["is_deleted"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    Vote,
    list_display=["id", "user_id", "post_id", "comment_id", "value", "created_at"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    AdminMessage,
    list_display=[
        "id",
        "from_user_id",
        "to_user_id",
        "subject",
        "is_read",
        "created_at",
    ],
    search_fields=["subject", "body"],
    list_filter=["is_read"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    SpamReport,
    list_display=["id", "reporter_id", "post_id", "comment_id", "status", "created_at"],
    search_fields=["reason"],
    list_filter=["status"],
    readonly_fields=["id", "created_at"],
    actions=[
        Action(
            name="mark_reviewed", label="Mark as reviewed", handler=mark_spam_reviewed
        ),
        Action(
            name="delete_spam",
            label="Delete spam posts",
            handler=delete_spam_posts,
            confirm=True,
        ),
    ],
)

admin.register(
    Forum,
    list_display=[
        "id",
        "name",
        "title",
        "is_public",
        "subscriber_count",
        "post_count",
        "created_at",
    ],
    search_fields=["name", "title", "description"],
    list_filter=["is_public"],
    readonly_fields=["id", "subscriber_count", "post_count", "created_at"],
)

admin.register(
    ForumMember,
    list_display=["id", "forum_id", "user_id", "role", "joined_at"],
    list_filter=["role"],
    readonly_fields=["id", "joined_at"],
)

admin.register(
    Bookmark,
    list_display=["id", "user_id", "post_id", "comment_id", "created_at"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    Notification,
    list_display=[
        "id",
        "user_id",
        "type",
        "actor_id",
        "message",
        "is_read",
        "created_at",
    ],
    list_filter=["type", "is_read"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    UserProfile,
    list_display=["id", "user_id", "website", "location", "github_username"],
    readonly_fields=["id"],
)

admin.register(
    PostRevision,
    list_display=["id", "post_id", "edited_by", "edit_reason", "created_at"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    Poll,
    list_display=["id", "post_id", "question", "poll_type", "created_at"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    UserFlair,
    list_display=["id", "user_id", "forum_id", "text", "css_class", "assigned_by"],
    readonly_fields=["id"],
)

admin.register(
    Award,
    list_display=["id", "post_id", "comment_id", "user_id", "award_type", "created_at"],
    list_filter=["award_type"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    Sequence,
    list_display=["id", "title", "author_id", "is_public", "created_at"],
    search_fields=["title"],
    readonly_fields=["id", "created_at"],
)

admin.register(
    AutomodRule,
    list_display=["id", "forum_id", "trigger", "action", "is_active", "created_at"],
    list_filter=["trigger", "action", "is_active"],
    readonly_fields=["id", "created_at"],
)

# Register StatusEvent list page + timeline admin actions
register_timeline_admin(admin)

# Register RBAC self-management (users, groups, permissions, audit, policy)
admin.register_auth_models()

# Register rate limit rule management
admin.register_ratelimit_models()

# Register cache monitoring dashboard
admin.register_cache_dashboard()


# ---------------------------------------------------------------------------
# Limits — configurable input constraints
# ---------------------------------------------------------------------------

MAX_TITLE_LENGTH = 300
MAX_POST_TEXT_LENGTH = 10_000
MAX_URL_LENGTH = 2_000
MAX_COMMENT_LENGTH = 5_000
MAX_DISPLAY_NAME_LENGTH = 60
MAX_BIO_LENGTH = 500
MAX_EMAIL_LENGTH = 254
MAX_MESSAGE_SUBJECT_LENGTH = 200
MAX_MESSAGE_BODY_LENGTH = 2_000
MAX_SPAM_REASON_LENGTH = 500
MAX_COMMENT_DEPTH = 10
POSTS_PER_PAGE = 30

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(text):
    """Convert text to URL-safe slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", text).strip("-")


def domain_from_url(url):
    """Extract display domain from URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc
    domain = domain.removeprefix("www.")
    return domain


async def validate_form(request, schema_cls: type):
    """Parse form data and validate against a BaseModel schema.

    Flattens multi-value form data (dict[str, list[str]]) to single values,
    then uses model_validate_strings() for automatic type coercion
    (str→int, str→bool, etc.) with Zig-accelerated validation.

    Returns validated schema instance.
    Raises ValidationErrors on invalid input.
    """
    raw = await request.form()
    flat: dict[str, str] = {}
    for key, val in raw.items():
        if key == "_csrf_token":
            continue
        if isinstance(val, list):
            flat[key] = val[0] if val else ""
        else:
            flat[key] = val
    return schema_cls.model_validate_strings(flat)


@time_bucket_cached(bucket_seconds=30)
def time_ago(timestamp_str):
    """Convert a timestamp string to a human-readable 'X ago' string.

    Cached within a 30-second bucket — the same timestamp passed
    multiple times within 30s returns the cached string. Hypernews
    list/detail pages hit this 38-51 times per request with heavy
    input repetition, so the cache eliminates most redundant work.
    """
    if not timestamp_str:
        return ""
    try:
        if isinstance(timestamp_str, str):
            ts = datetime.fromisoformat(timestamp_str)
        else:
            ts = timestamp_str
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        diff = now - ts
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        if days < 30:
            return f"{days} day{'s' if days != 1 else ''} ago"
        months = days // 30
        if months < 12:
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
    except ValueError, TypeError:
        return ""


# ---------------------------------------------------------------------------
# Guard resolver: active user (replaces @guard(*REQUIRE_ACTIVE) decorator)
# ---------------------------------------------------------------------------


async def _resolve_active_user(request, ctx):
    """Load the full User object and check ban/mute status.

    Used as a guard resource resolver to provide request.guard.active_user.
    Raises HTTPException(403) for banned/muted users. Returns None if the
    user no longer exists in DB (triggers 404 from guard).

    Uses active_statuses() for a single DB query covering all status checks.
    """
    user_id = request.user.id if request.user is not None else None
    if user_id is None:
        return None
    user = await User.objects.filter(id=user_id).first()
    if not user:
        return None
    statuses = await user.active_statuses()
    if "banned" in statuses:
        raise HTTPException(403, "Your account has been banned")
    if "muted" in statuses:
        raise HTTPException(403, "Your account has been muted")
    return user


# Reusable requirement chains — declared once, used across all routes.
REQUIRE_LOGIN = (Require.authenticated(redirect_url="/login"),)
REQUIRE_ACTIVE = (
    Require.authenticated(redirect_url="/login"),
    Require.resource("active_user", resolver=_resolve_active_user),
)


def get_uid(request) -> int:
    """Extract authenticated user ID from request session.

    Returns 0 if not authenticated. Use in routes behind @guard(*REQUIRE_LOGIN)
    or @guard(*REQUIRE_ACTIVE) where authentication is guaranteed.
    """
    return (request.user.id or 0) if request.user is not None else 0


def get_uid_or_none(request) -> int | None:
    """Extract user ID, returning None for anonymous users.

    Use in routes that serve both anonymous and authenticated users
    (e.g., viewing content where membership checks are optional).
    """
    return request.user.id if request.user is not None else None


async def get_is_staff(request) -> bool:
    """Check if current user has staff access via timeline."""
    if request.user is None or not request.user.is_authenticated:
        return False
    user_id = request.user.id
    if not user_id:
        return False
    tl = get_timeline()
    return await tl.is_active("user", user_id, "staff")


async def get_current_user(request):
    """Get the full User object for the logged-in user, or None.

    Memoized via LocMemCache with 2s TTL to avoid redundant DB queries
    when multiple callsites fetch the same user within a single request cycle.
    """
    if not request.user:
        return None
    user_id = get_uid_or_none(request)
    if user_id is None:
        return None
    cache_key = f"_user:{user_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    user = await User.objects.filter(id=user_id).first()
    if user:
        _cache.set(cache_key, user, ttl=2)
    return user


def build_context(request, **extra):
    """Build a base template context with user info and external IDs."""
    user_data = request.user
    # Derive is_staff from session groups (RBAC), not boolean flags
    groups = user_data.get("groups") if user_data is not None else None
    is_staff = isinstance(groups, list) and "staff" in groups
    ctx = {
        "user": user_data,
        "is_staff": is_staff,
        "csrf_token": request.cookies.get("csrftoken", ""),
        "request": request,
        "site": _site_config,
        "site_css": (
            _site_config.theme.to_css_vars()
            + f"\n:root {{ --font-family: {_site_config.font_family};"
            + f" --base-font-size: {_site_config.base_font_size}; }}"
        ),
    }
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Helpers: Forum context
# ---------------------------------------------------------------------------

FORUMS_PER_PAGE = 30
MIN_KARMA_TO_CREATE_FORUM = 50

_MOD_ROLES = frozenset({ForumRole.MODERATOR.value, ForumRole.ADMIN.value})


def _normalize_role(role) -> str:
    """Normalize ForumRole enum or string to string value."""
    return role.value if isinstance(role, ForumRole) else role


def _normalize_status(status) -> str:
    """Normalize PostStatus enum or string to string value."""
    return status.value if isinstance(status, PostStatus) else (status or "published")


async def get_forum_by_name(name: str) -> Forum | None:
    """Fetch a forum by its URL slug name."""
    return await Forum.objects.filter(name=name).first()


async def _visible_forum_ids(uid: int | None) -> set[int]:
    """Compute set of forum IDs visible to this user (public + member of).

    Always includes 0 (global/no-forum posts).
    Cached for 30s per user to avoid per-request ORM queries.

    Hidden forums (forums with an active "hidden" status event) are
    excluded from the public list but STILL included if the user is
    a member — so moderators/members still see their own spaces.
    Uses :class:`Exists` + :class:`OuterRef` (task #197) to filter
    out forums that have an active hidden status event via a correlated
    NOT EXISTS subquery. Previously used a raw SQL string for the
    visibility filter inside other handlers; this helper didn't apply
    the filter at all — a latent divergence that would leak hidden
    forums into the visible set for non-members.
    """
    cache_key = f"_visible:{uid or 0}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    public_forums = (
        await Forum.objects.filter(is_public=True)
        .exclude(
            Exists(
                StatusEvent.objects.filter(
                    entity_type="forum",
                    entity_id=OuterRef("id"),
                    status="hidden",
                    ended_at=None,
                )
            )
        )
        .all()
    )
    ids = {0} | {f.id for f in public_forums}
    if uid:
        memberships = await ForumMember.objects.filter(user_id=int(uid)).all()
        ids |= {m.forum_id for m in memberships}
    _cache.set(cache_key, ids, ttl=30)
    return ids


async def get_membership(user_id: int, forum_id: int) -> tuple[bool, bool]:
    """Check membership and mod status in a single query.

    Returns (is_member, is_mod).
    """
    member = await ForumMember.objects.filter(
        forum_id=forum_id, user_id=user_id
    ).first()
    if not member:
        return False, False
    return True, _normalize_role(member.role) in _MOD_ROLES


async def _forum_to_dict(forum: Forum) -> dict:
    """Convert Forum model to template-safe dict with timeline-derived statuses."""
    d = forum.to_dict()
    # Add computed timeline statuses (not stored as columns)
    d["is_archived"] = await forum.has_status("archive", "archived")
    d["is_locked"] = await forum.has_status("lock", "locked")
    d["is_hidden"] = await forum.has_status("visibility", "hidden")
    return d


# ---------------------------------------------------------------------------
# Purpose-based access control: resolve_forum / resolve_post
# ---------------------------------------------------------------------------


class ForumIntent(Enum):
    """What the caller intends to do with the forum.

    Each intent implies a set of access checks — the resolver enforces all
    of them in one call, eliminating scattered manual is_archived/is_locked checks.
    """

    READ = "read"  # View forum, list posts
    WRITE_POST = "write_post"  # Submit or crosspost (rejects archived+locked)
    WRITE_COMMENT = "write_comment"  # Add comment (rejects archived+locked)
    MODERATE = "moderate"  # Pin, flair others, etc. (requires mod/admin)
    ADMIN = "admin"  # Settings, automod, member management


@dataclass
class ForumAccess:
    """Result of resolving and validating forum access by intent."""

    forum: Forum
    is_member: bool
    is_mod: bool
    membership: ForumMember | None  # None if not a member


_WRITE_INTENTS = frozenset({ForumIntent.WRITE_POST, ForumIntent.WRITE_COMMENT})


async def resolve_forum(request, forum_name: str, intent: ForumIntent) -> ForumAccess:
    """Fetch a forum and enforce intent-specific access control.

    Centralizes the scattered pattern of get_forum_by_name → null check →
    require_public_or_member → is_archived → is_locked → require_forum_admin
    into ONE call with intent-driven checks.

    Intent rules:
        READ:          public/member check, reject hidden (unless member)
        WRITE_POST:    READ + reject archived + reject locked
        WRITE_COMMENT: READ + reject archived + reject locked
        MODERATE:      READ + require mod/admin role
        ADMIN:         READ + require admin role or site staff

    Returns ForumAccess with validated forum and membership info.
    Raises HTTPException on any access violation.
    """
    forum = await get_forum_by_name(forum_name)
    if not forum:
        raise HTTPException(404, "Forum not found")

    # Resolve membership in one query
    uid = get_uid_or_none(request)
    is_member = False
    is_mod = False
    membership = None

    if uid:
        member = await ForumMember.objects.filter(
            forum_id=forum.id, user_id=uid
        ).first()
        if member:
            is_member = True
            membership = member
            role = _normalize_role(member.role)
            is_mod = role in _MOD_ROLES

    # Hidden forum — only members can see it
    if await forum.has_status("visibility", "hidden") and not is_member:
        raise HTTPException(404, "Forum not found")

    # Private forum — require membership for all intents
    if not forum.is_public and not is_member:
        raise HTTPException(403, "This forum is private")

    # Write intents — reject archived and locked
    if intent in _WRITE_INTENTS:
        if await forum.has_status("archive", "archived"):
            raise HTTPException(403, "This forum is archived")
        if await forum.has_status("lock", "locked"):
            raise HTTPException(403, "This forum is not accepting new posts")

    # Moderate — require mod or admin role (or site staff)
    if intent == ForumIntent.MODERATE:
        if not is_mod:
            user = await get_current_user(request)
            if not user or not await user.has_status("access", "staff"):
                raise HTTPException(403, "Moderator access required")

    # Admin — require admin role or site staff
    if intent == ForumIntent.ADMIN:
        is_admin = (
            membership and _normalize_role(membership.role) == ForumRole.ADMIN.value
        )
        if not is_admin:
            user = await get_current_user(request)
            if not user or not await user.has_status("access", "staff"):
                raise HTTPException(403, "Only forum admins can perform this action")

    return ForumAccess(forum, is_member, is_mod, membership)


# Guard-compatible resolver factories for each intent.
# resolve_forum() already raises HTTPException, which the guard passes through.
def _make_forum_resolver(intent: ForumIntent):
    """Create a guard resource resolver for a specific forum intent."""

    async def resolver(request, ctx, forum_name):
        return await resolve_forum(request, forum_name, intent)

    return resolver


_resolve_forum_read = _make_forum_resolver(ForumIntent.READ)
_resolve_forum_write = _make_forum_resolver(ForumIntent.WRITE_POST)
_resolve_forum_write_comment = _make_forum_resolver(ForumIntent.WRITE_COMMENT)
_resolve_forum_moderate = _make_forum_resolver(ForumIntent.MODERATE)
_resolve_forum_admin = _make_forum_resolver(ForumIntent.ADMIN)


@dataclass
class PostAccess:
    """Result of resolving and validating a post by its external PID.

    Centralizes: decode PID → fetch post → check deleted → check draft visibility →
    check forum exists → check forum state → check private membership.
    Every route that operates on a post by PID uses this instead of duplicating checks.
    """

    post: Post
    forum: Forum | None  # None for global posts (forum_id=0)


async def resolve_post(
    request,
    pid: str,
    *,
    require_author: bool = False,
    require_published: bool = False,
    require_not_archived: bool = False,
    require_not_locked: bool = False,
) -> PostAccess:
    """Resolve an external post ID with full access control.

    Args:
        pid: External (HMAC-signed) post ID from URL path.
        require_author: If True, caller must be the post author (or staff).
        require_published: If True, reject draft posts unless caller is author.
        require_not_archived: If True, reject if forum is archived (for edits).
        require_not_locked: If True, reject if forum is locked (for new content).

    Returns PostAccess with validated post and optional forum.
    Raises HTTPException on any access violation.
    """
    try:
        post_id = Post.decode_external_id(pid)
    except ValueError:
        raise HTTPException(404, "Post not found")

    post = await Post.objects.filter(id=post_id).first()
    if not post or post.is_deleted:
        raise HTTPException(404, "Post not found")

    # Draft visibility — only author can see their own drafts
    post_status = _normalize_status(post.status)
    if post_status == "draft":
        uid = get_uid_or_none(request)
        if uid != post.author_id:
            raise HTTPException(404, "Post not found")
    elif require_published and post_status != "published":
        raise HTTPException(400, "Post is not published")

    # Forum access — exists, private membership, writable state
    forum = None
    if post.forum_id:
        forum = await Forum.objects.filter(id=post.forum_id).first()
        if not forum:
            raise HTTPException(404, "Post not found")
        # Inline private forum check (can't use resolve_forum since we have forum by ID)
        if not forum.is_public:
            uid_check = get_uid_or_none(request)
            if not uid_check:
                raise HTTPException(403, "This forum is private")
            is_member, _ = await get_membership(uid_check, forum.id)
            if not is_member:
                raise HTTPException(403, "This forum is private")
        if require_not_archived and await forum.has_status("archive", "archived"):
            raise HTTPException(403, "This forum is archived")
        if require_not_locked and await forum.has_status("lock", "locked"):
            raise HTTPException(403, "This forum is locked")

    # Author check
    if require_author:
        uid = get_uid_or_none(request)
        is_staff = await get_is_staff(request)
        if uid != post.author_id and not is_staff:
            raise HTTPException(403, "You can only modify your own posts")

    return PostAccess(post, forum)


def _make_post_resolver(**flags):
    """Create a guard resource resolver for resolve_post with specific flags."""

    async def resolver(request, ctx, pid):
        return await resolve_post(request, pid, **flags)

    return resolver


# Common post resolver variants
_resolve_post_view = _make_post_resolver()
_resolve_post_author = _make_post_resolver(require_author=True)
_resolve_post_edit = _make_post_resolver(
    require_author=True, require_not_archived=True, require_not_locked=True
)
_resolve_post_publish = _make_post_resolver(
    require_author=True, require_published=False
)
_resolve_post_published = _make_post_resolver(require_published=True)
_resolve_post_author_not_archived = _make_post_resolver(
    require_author=True, require_not_archived=True
)


async def require_target_forum_access(
    request, db, post_id: int = 0, comment_id: int = 0
) -> int:
    """Verify the caller can access the forum that a post or comment belongs to.

    Resolves comment_id → post_id → forum_id in a single query via JOIN.
    Used by all write operations (vote, comment, report, tag, agree) that
    accept raw post_id/comment_id to prevent actions on private forum content.
    Returns the forum_id (0 for global posts or when no target specified).
    """
    # Resolve to forum in one query: comment → post → forum (LEFT JOIN handles global posts)
    if post_id:
        row = await db.query_tuples(
            """SELECT p.forum_id, f.is_public
               FROM hn_posts p LEFT JOIN hn_forums f ON f.id = p.forum_id
               WHERE p.id = $1""",
            post_id,
        )
    elif comment_id:
        row = await db.query_tuples(
            """SELECT p.forum_id, f.is_public
               FROM hn_comments c JOIN hn_posts p ON p.id = c.post_id
               LEFT JOIN hn_forums f ON f.id = p.forum_id
               WHERE c.id = $1""",
            comment_id,
        )
    else:
        return 0
    if not row:
        raise HTTPException(404, "Content not found")
    forum_id, is_public = row[0]
    if not forum_id:
        return 0  # Global post — no forum restriction
    if is_public:
        return forum_id  # Public forum — no membership needed
    # Private forum — must be a member
    uid = get_uid_or_none(request)
    if not uid:
        raise HTTPException(403, "This content is in a private forum")
    is_member, _ = await get_membership(uid, forum_id)
    if not is_member:
        raise HTTPException(403, "This content is in a private forum")
    return forum_id


async def _notify(
    user_id: int,
    ntype: NotificationType,
    message: str,
    actor_id: int = 0,
    post_id: int = 0,
    comment_id: int = 0,
) -> None:
    """Create a notification for a user. Skips if actor == recipient (no self-notifs)."""
    if actor_id == user_id:
        return
    await Notification(
        user_id=user_id,
        type=ntype,
        actor_id=actor_id,
        post_id=post_id,
        comment_id=comment_id,
        message=message,
    ).save()


def _extract_mentions(text: str) -> list[str]:
    """Extract @username mentions from text. Returns list of unique usernames."""
    return list(dict.fromkeys(re.findall(r"@([a-zA-Z0-9_-]+)", text)))


async def _log_forum_action(
    moderator_id: int,
    forum_id: int,
    action: str,
    reason: str = "",
    target_user_id: int = 0,
) -> None:
    """Write an auditable ModAction record for a forum-level operation."""
    await ModAction(
        moderator_id=moderator_id,
        forum_id=forum_id,
        action=action,
        reason=reason,
        target_user_id=target_user_id,
    ).save()


async def build_forum_context(request, forum: Forum, **extra):
    """Build template context with forum info included."""
    ctx = build_context(request)
    ctx["forum"] = await _forum_to_dict(forum)
    ctx.update(extra)
    return ctx


async def _query_posts_paginated(
    db,
    tab: str,
    cursor_param: str,
    per_page: int,
    forum_id: int = 0,
    visible_forum_ids: set[int] | None = None,
):
    """Unified post query with keyset pagination. Optional forum_id filter.

    Args:
        forum_id: If >0, scope to this forum. If 0, aggregate across forums.
        visible_forum_ids: When aggregating (forum_id=0), restrict to these forum IDs
                          to enforce private forum isolation. If None, no filter.

    Returns (post_rows, has_more, next_cursor, prev_cursor).
    """
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None

    # Build WHERE clause — always exclude deleted and drafts
    where = "is_deleted = false AND (status = 'published' OR status IS NULL) AND NOT COALESCE(is_pinned, false)"
    base_params: list = []
    param_offset = 0

    if forum_id:
        param_offset = 1
        where += " AND forum_id = $1"
        base_params.append(forum_id)
    elif visible_forum_ids is not None:
        # Aggregated mode — filter to visible forums only (parameterized array)
        param_offset = 1
        where += " AND forum_id = ANY($1::int[])"
        base_params.append(list(visible_forum_ids))

    if tab == "ask":
        where += " AND is_ask = true"

    # Column lists — forum_id at index 12 (before any extra_col)
    base_cols = """id, title, slug, url, text, author_id, score, comment_count,
                   is_ask, is_show, is_deleted, created_at, forum_id"""
    extra_col = ""
    order_cols = ""
    cursor_build_fn = None

    if tab == "new":
        order_cols = "created_at DESC, id DESC"

        def cursor_build_fn(row):
            return f"{row[11].isoformat()}|{row[0]}"

    elif tab == "top":
        order_cols = "score DESC, created_at DESC, id DESC"

        def cursor_build_fn(row):
            return f"{row[6]}|{row[11].isoformat()}|{row[0]}"

    elif tab == "controversial":
        extra_col = ", controversy"
        order_cols = "controversy DESC, id DESC"
        where += " AND (upvotes + downvotes) >= 2"

        def cursor_build_fn(row):
            return f"{row[13]}|{row[0]}"

    elif tab == "rising":
        extra_col = ", velocity"
        order_cols = "velocity DESC, id DESC"
        where += " AND created_at > NOW() - INTERVAL '24 hours'"

        def cursor_build_fn(row):
            return f"{row[13]}|{row[0]}"

    elif tab == "ask":
        order_cols = "created_at DESC, id DESC"

        def cursor_build_fn(row):
            return f"{row[11].isoformat()}|{row[0]}"

    else:  # hot (default)
        extra_col = ", hot_score"
        order_cols = "hot_score DESC, id DESC"

        def cursor_build_fn(row):
            return f"{row[13]}|{row[0]}"

    # Build keyset WHERE from cursor
    keyset_where = ""
    keyset_params: list = []
    if cursor_data and cursor_data[0] == "next":
        parts = str(cursor_data[1]).split("|", 2)
        if tab == "new" or tab == "ask":
            if len(parts) == 2:
                p = param_offset
                keyset_where = (
                    f" AND (created_at, id) < (${p + 1}::timestamptz, ${p + 2}::int)"
                )
                keyset_params = [parts[0], int(parts[1])]
        elif tab == "top":
            if len(parts) == 3:
                p = param_offset
                keyset_where = f" AND (score, created_at, id) < (${p + 1}::int, ${p + 2}::timestamptz, ${p + 3}::int)"
                keyset_params = [int(parts[0]), parts[1], int(parts[2])]
        elif tab in ("controversial", "rising", "hot"):
            if len(parts) == 2:
                p = param_offset
                col = {
                    "controversial": "controversy",
                    "rising": "velocity",
                    "hot": "hot_score",
                }[tab]
                keyset_where = f" AND ({col}, id) < (${p + 1}::float8, ${p + 2}::int)"
                keyset_params = [float(parts[0]), int(parts[1])]

    all_params = base_params + keyset_params
    limit_param_idx = len(all_params) + 1
    all_params.append(per_page + 1)

    sql = f"""SELECT {base_cols}{extra_col}
              FROM hn_posts WHERE {where}{keyset_where}
              ORDER BY {order_cols} LIMIT ${limit_param_idx}"""

    rows = await db.query_tuples(sql, *all_params)

    has_more = len(rows) > per_page
    rows = rows[:per_page]

    next_cursor = ""
    if has_more and rows:
        next_cursor = _encode_cursor("next", cursor_build_fn(rows[-1]))

    prev_cursor = ""
    if cursor_param and rows:
        prev_cursor = _encode_cursor("prev", cursor_build_fn(rows[0]))

    return rows, has_more, next_cursor, prev_cursor


async def _rows_to_post_list(rows: list) -> list[dict]:
    """Convert raw post tuples to template-ready dicts with batch author + forum lookup.

    Column layout: 0=id, 1=title, 2=slug, 3=url, 4=text, 5=author_id, 6=score,
    7=comment_count, 8=is_ask, 9=is_show, 10=is_deleted, 11=created_at, 12=forum_id,
    [13=extra_col if present]
    """
    # Batch fetch authors
    author_ids = list({r[5] for r in rows})
    authors: dict[int, str] = {}
    if author_ids:
        author_rows = await User.objects.filter(id__in=author_ids).all()
        authors = {u.id: u.username for u in author_rows}

    # Batch fetch forum names (forum_id is at index 12)
    forum_ids = list({r[12] for r in rows if r[12]})
    forums: dict[int, str] = {}
    if forum_ids:
        forum_rows = await Forum.objects.filter(id__in=forum_ids).all()
        forums = {f.id: f.name for f in forum_rows}

    post_list = []
    for i, r in enumerate(rows):
        post_obj = Post(id=r[0])
        forum_id = r[12]
        forum_name = forums.get(forum_id, "") if forum_id else ""
        post_list.append(
            {
                "rank": i + 1,
                "id": r[0],
                "pid": post_obj.get_external_id(),
                "title": r[1],
                "slug": r[2],
                "url": r[3],
                "domain": domain_from_url(r[3]),
                "score": r[6],
                "author": authors.get(r[5], "unknown"),
                "comment_count": r[7],
                "time_ago": time_ago(r[11]),
                "is_ask": r[8],
                "forum_name": forum_name,
            }
        )
    return post_list


# ---------------------------------------------------------------------------
# Routes: Front page (aggregated from all public forums)
# ---------------------------------------------------------------------------


@app.get("/")
async def index(request):
    """Front page — aggregated posts from all public forums.

    Supports 6 tabs with HMAC-signed keyset cursor pagination.
    Cursor-based pagination is O(1) regardless of depth — no OFFSET scanning.
    """
    tab = request.GET.get("tab", "hot")
    cursor_param = request.GET.get("cursor", "")
    per_page = POSTS_PER_PAGE
    db = get_db()

    # Compute visible forums (public + user's memberships) for private isolation
    uid = get_uid_or_none(request)
    visible = await _visible_forum_ids(uid)

    # Cache per cursor+tab+user — 10s TTL. Keyed by user to respect private forum access.
    cache_key = f"index:{tab}:{cursor_param or 'first'}:u{uid or 0}"
    cached = _cache.get(cache_key)
    if cached is not None:
        post_list, next_cursor, prev_cursor = cached
    else:
        rows, has_more, next_cursor, prev_cursor = await _query_posts_paginated(
            db, tab, cursor_param, per_page, visible_forum_ids=visible
        )
        post_list = await _rows_to_post_list(rows)
        _cache.set(cache_key, (post_list, next_cursor, prev_cursor), ttl=10)

    ctx = build_context(
        request,
        posts=post_list,
        tab=tab,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
    return app.render("index.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Forum directory
# ---------------------------------------------------------------------------


@app.get("/forums")
async def forum_directory(request):
    """Browsable forum directory sorted by subscriber count or creation date."""
    sort = request.GET.get("sort", "popular")
    cursor_param = request.GET.get("cursor", "")
    db = get_db()

    # Sort config — one place for both keyset column and param types
    _FORUM_COLS = """id, name, title, description, is_public, subscriber_count,
                     post_count, created_at"""
    if sort == "new":
        order = "created_at DESC, id DESC"
        keyset_col = "(created_at, id)"
        cast = ("$1::timestamptz", "$2::int")
        parse_parts = lambda parts: (parts[0], int(parts[1]))
        cursor_val_fn = lambda r: f"{r[7].isoformat()}|{r[0]}"
    else:
        order = "subscriber_count DESC, id DESC"
        keyset_col = "(subscriber_count, id)"
        cast = ("$1::int", "$2::int")
        parse_parts = lambda parts: (int(parts[0]), int(parts[1]))
        cursor_val_fn = lambda r: f"{r[5]}|{r[0]}"

    # Parse cursor
    keyset_where = ""
    params: list = []
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None
    if cursor_data and cursor_data[0] == "next":
        parts = str(cursor_data[1]).split("|", 1)
        if len(parts) == 2:
            p1, p2 = parse_parts(parts)
            keyset_where = f" AND {keyset_col} < ({cast[0]}, {cast[1]})"
            params = [p1, p2]

    limit_idx = len(params) + 1
    params.append(FORUMS_PER_PAGE + 1)

    rows = await db.query_tuples(
        f"""SELECT {_FORUM_COLS}
            FROM hn_forums f WHERE f.is_public = true
            AND NOT EXISTS (
                SELECT 1 FROM hyper_status_events e
                WHERE e.entity_type = 'forum' AND e.entity_id = f.id
                AND e.status = 'hidden' AND e.ended_at IS NULL
                AND (e.expires_at IS NULL OR e.expires_at > now())
            ){keyset_where}
            ORDER BY {order} LIMIT ${limit_idx}""",
        *params,
    )

    has_more = len(rows) > FORUMS_PER_PAGE
    rows = rows[:FORUMS_PER_PAGE]

    forum_list = [
        {
            "id": r[0],
            "name": r[1],
            "title": r[2],
            "description": r[3][:200] if r[3] else "",
            "subscriber_count": r[5],
            "post_count": r[6],
            "time_ago": time_ago(r[7]),
        }
        for r in rows
    ]

    next_cursor = ""
    if has_more and rows:
        next_cursor = _encode_cursor("next", cursor_val_fn(rows[-1]))

    ctx = build_context(
        request,
        forums=forum_list,
        sort=sort,
        next_cursor=next_cursor,
    )
    return app.render("forums.html", ctx)


@app.get("/forums/create")
@guard(*REQUIRE_LOGIN)
async def create_forum_form(request):
    """Forum creation form (karma-gated)."""
    user = await get_current_user(request)
    if (
        user
        and user.karma < MIN_KARMA_TO_CREATE_FORUM
        and not await user.has_status("access", "staff")
    ):
        ctx = build_context(
            request,
            error=f"You need at least {MIN_KARMA_TO_CREATE_FORUM} karma to create a forum (you have {user.karma})",
        )
        return app.render("create_forum.html", ctx, status=403)
    ctx = build_context(request)
    return app.render("create_forum.html", ctx)


@app.post("/forums/create")
@guard(*REQUIRE_ACTIVE)
async def create_forum_handler(request):
    """Handle forum creation."""
    user = request.guard.active_user
    if user.karma < MIN_KARMA_TO_CREATE_FORUM and not await user.has_status(
        "access", "staff"
    ):
        raise HTTPException(
            403, f"Need {MIN_KARMA_TO_CREATE_FORUM} karma to create forums"
        )

    check_action_rate_limit(user.id, "forum_create", 10, 3600)

    try:
        data = await validate_form(request, CreateForumSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("create_forum.html", ctx, status=400)

    name = data.name
    title = data.title
    description = data.description
    rules = data.rules

    # Business logic: DB uniqueness check. `.exists()` uses
    # SELECT 1 ... LIMIT 1 and is cheaper than pulling the full row
    # just to check presence.
    if await Forum.objects.filter(name=name).exists():
        ctx = build_context(
            request,
            errors=["A forum with that name already exists"],
            name=name,
            title=title,
            description=description,
            rules=rules,
        )
        return app.render("create_forum.html", ctx, status=400)

    db = get_db()
    try:
        async with db.transaction():
            forum = Forum(
                name=name,
                title=title,
                description=description,
                rules=rules,
                created_by=user.id,
                subscriber_count=1,
            )
            await forum.save()
            # Creator is auto-admin
            await ForumMember(
                forum_id=forum.id,
                user_id=user.id,
                role=ForumRole.ADMIN,
            ).save()
    except IntegrityError:
        # Race condition: another request created the same forum name
        ctx = build_context(
            request,
            errors=["A forum with that name already exists"],
            name=name,
            title=title,
            description=description,
            rules=rules,
        )
        return app.render("create_forum.html", ctx, status=400)

    return Response.redirect(f"/f/{name}/")


# ---------------------------------------------------------------------------
# Routes: Forum home (/f/{name}/)
# ---------------------------------------------------------------------------


@app.get("/f/{forum_name}/")
async def forum_home(request, forum_name: str):
    """Forum front page with tab-sorted posts, scoped to this forum."""
    try:
        access = await resolve_forum(request, forum_name, ForumIntent.READ)
    except HTTPException as exc:
        if exc.status_code == 404:
            return app.render("404.html", build_context(request), status=404)
        raise
    forum = access.forum

    tab = request.GET.get("tab", "hot")
    cursor_param = request.GET.get("cursor", "")
    per_page = POSTS_PER_PAGE
    db = get_db()

    cache_key = f"forum:{forum_name}:{tab}:{cursor_param or 'first'}"
    cached = _cache.get(cache_key)
    if cached is not None:
        pinned_list, post_list, next_cursor, prev_cursor = cached
    else:
        # Pinned posts — always at top, separate from pagination
        pinned_rows = await db.query_tuples(
            """SELECT id, title, slug, url, text, author_id, score, comment_count,
                      is_ask, is_show, is_deleted, created_at, forum_id
               FROM hn_posts WHERE forum_id = $1 AND is_pinned = true AND NOT is_deleted
                 AND (status = 'published' OR status IS NULL)
               ORDER BY created_at DESC""",
            forum.id,
        )
        pinned_list = await _rows_to_post_list(pinned_rows)
        for p in pinned_list:
            p["is_pinned"] = True

        rows, has_more, next_cursor, prev_cursor = await _query_posts_paginated(
            db, tab, cursor_param, per_page, forum_id=forum.id
        )
        post_list = await _rows_to_post_list(rows)
        _cache.set(
            cache_key, (pinned_list, post_list, next_cursor, prev_cursor), ttl=10
        )

    ctx = await build_forum_context(
        request,
        forum,
        pinned_posts=pinned_list,
        posts=post_list,
        tab=tab,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        is_member=access.is_member,
        is_mod=access.is_mod,
    )
    return app.render("forum_home.html", ctx)


@app.get("/f/{forum_name}/about")
async def forum_about(request, forum_name: str):
    """Forum about page — description, rules, mod list."""
    try:
        access = await resolve_forum(request, forum_name, ForumIntent.READ)
    except HTTPException as exc:
        if exc.status_code == 404:
            return app.render("404.html", build_context(request), status=404)
        raise
    forum = access.forum

    # Fetch moderators and admins — SQL-level filter, not Python
    db = get_db()
    mod_rows = await db.query_tuples(
        """SELECT fm.user_id, fm.role, u.username
           FROM hn_forum_members fm JOIN hn_users u ON u.id = fm.user_id
           WHERE fm.forum_id = $1 AND fm.role IN ($2, $3)
           ORDER BY fm.role, u.username""",
        forum.id,
        ForumRole.ADMIN.value,
        ForumRole.MODERATOR.value,
    )

    mod_list = [{"username": r[2], "role": r[1]} for r in mod_rows]

    # Check if current user can edit — reuse membership from resolve_forum
    is_forum_admin = (
        access.membership
        and _normalize_role(access.membership.role) == ForumRole.ADMIN.value
    )
    can_edit = is_forum_admin or await get_is_staff(request)

    ctx = await build_forum_context(
        request, forum, moderators=mod_list, can_edit=can_edit
    )
    return app.render("forum_about.html", ctx)


@app.get("/f/{forum_name}/edit")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_edit_form(request, forum_name: str):
    """Forum settings form (forum admin or site staff only)."""
    access = request.guard.access
    forum = access.forum
    ctx = await build_forum_context(request, forum)
    return app.render("forum_edit.html", ctx)


@app.post("/f/{forum_name}/edit")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_edit_handler(request, forum_name: str):
    """Handle forum settings update."""
    access = request.guard.access
    forum = access.forum

    try:
        data = await validate_form(request, ForumEditSchema)
    except ValidationErrors as exc:
        ctx = await build_forum_context(
            request, forum, errors=[str(e) for e in exc.errors]
        )
        return app.render("forum_edit.html", ctx, status=400)

    title = data.title
    description = data.description
    rules = data.rules
    is_public = data.is_public == "on"
    want_archived = data.is_archived == "on"
    want_locked = data.is_locked == "on"
    want_hidden = data.is_hidden == "on"

    # Build change description for audit log
    uid = get_uid(request)
    changes = []
    if forum.title != title:
        changes.append(f"title: {forum.title!r} -> {title!r}")
    if forum.is_public != is_public:
        changes.append(f"public: {is_public}")
    if forum.description != description:
        changes.append("description updated")
    if forum.rules != rules:
        changes.append("rules updated")

    # Update model fields (is_public stays as a field)
    await Forum.objects.filter(id=forum.id).update(
        title=title,
        description=description,
        rules=rules,
        is_public=is_public,
    )

    # Update timeline statuses for archived/locked/hidden
    cur_archived = await forum.has_status("archive", "archived")
    cur_locked = await forum.has_status("lock", "locked")
    cur_hidden = await forum.has_status("visibility", "hidden")

    if want_archived and not cur_archived:
        await forum.set_status(
            "archive", "archived", reason="Forum settings", actor_id=uid
        )
        changes.append("archived: True")
    elif not want_archived and cur_archived:
        await forum.clear_status("archive", reason="Forum settings", actor_id=uid)
        changes.append("archived: False")

    if want_locked and not cur_locked:
        await forum.set_status("lock", "locked", reason="Forum settings", actor_id=uid)
        changes.append("locked: True")
    elif not want_locked and cur_locked:
        await forum.clear_status("lock", reason="Forum settings", actor_id=uid)
        changes.append("locked: False")

    if want_hidden and not cur_hidden:
        await forum.set_status(
            "visibility", "hidden", reason="Forum settings", actor_id=uid
        )
        changes.append("hidden: True")
    elif not want_hidden and cur_hidden:
        await forum.clear_status("visibility", reason="Forum settings", actor_id=uid)
        changes.append("hidden: False")
    if changes:
        await _log_forum_action(uid, forum.id, "forum_edit", reason="; ".join(changes))
    _cache.clear()
    return Response.redirect(f"/f/{forum_name}/about")


@app.post("/f/{forum_name}/join")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_read, from_path="forum_name"),
)
async def forum_join(request, forum_name: str):
    """Subscribe to a forum."""
    access = request.guard.access
    forum = access.forum
    uid = get_uid(request)
    if not uid:
        return Response.redirect("/login")
    check_action_rate_limit(uid, "join_leave", 10, 300)

    # Use access.membership from resolve_forum instead of querying again
    if access.membership:
        return Response.redirect(f"/f/{forum_name}/")

    db = get_db()
    try:
        async with db.transaction():
            await ForumMember(
                forum_id=forum.id, user_id=uid, role=ForumRole.SUBSCRIBER
            ).save()
            await Forum.objects.filter(id=forum.id).update(
                subscriber_count=F("subscriber_count") + 1
            )
    except IntegrityError:
        pass  # Already a member (race condition) — ignore
    _cache.clear()
    return Response.redirect(f"/f/{forum_name}/")


@app.post("/f/{forum_name}/leave")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_read, from_path="forum_name"),
)
async def forum_leave(request, forum_name: str):
    """Unsubscribe from a forum."""
    access = request.guard.access
    forum = access.forum
    uid = get_uid(request)
    check_action_rate_limit(uid, "join_leave", 10, 300)

    # Reuse membership from resolve_forum instead of querying again
    if not access.membership:
        return Response.redirect(f"/f/{forum_name}/")

    membership = access.membership
    # Don't let the last admin leave — single COUNT query, not fetch-all
    if _normalize_role(membership.role) == ForumRole.ADMIN.value:
        admin_count = await ForumMember.objects.filter(
            forum_id=forum.id,
            role=ForumRole.ADMIN.value,
        ).count()
        if admin_count <= 1:
            raise HTTPException(
                400, "Cannot leave — you are the last admin. Transfer ownership first."
            )

    db = get_db()
    async with db.transaction():
        await ForumMember.objects.filter(id=membership.id).delete()
        await Forum.objects.filter(id=forum.id).update(
            subscriber_count=F("subscriber_count") - 1
        )
    _cache.clear()
    return Response.redirect(f"/f/{forum_name}/")


@app.get("/f/{forum_name}/submit")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_write, from_path="forum_name"),
)
async def forum_submit_form(request, forum_name: str):
    """Show post submission form scoped to a forum."""
    access = request.guard.access
    forum = access.forum
    ctx = await build_forum_context(request, forum)
    return app.render("forum_submit.html", ctx)


@app.post("/f/{forum_name}/submit")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_write, from_path="forum_name"),
)
async def forum_submit_post(request, forum_name: str):
    """Handle post submission to a specific forum."""
    access = request.guard.access
    forum = access.forum

    user = request.guard.active_user
    check_action_rate_limit(user.id, "submit", 3, 60)

    try:
        data = await validate_form(request, ForumSubmitPostSchema)
    except ValidationErrors as exc:
        ctx = await build_forum_context(
            request, forum, errors=[str(e) for e in exc.errors]
        )
        return app.render("forum_submit.html", ctx, status=400)

    title = data.title
    url = data.url
    text = data.text

    # Cross-field check: url or text required
    if not url and not text:
        ctx = await build_forum_context(
            request,
            forum,
            errors=["Either URL or text is required"],
            title=title,
            url=url,
            text=text,
        )
        return app.render("forum_submit.html", ctx, status=400)

    is_ask = title.lower().startswith("ask hn:") or title.lower().startswith(
        "ask hypernews:"
    )
    is_show = title.lower().startswith("show hn:") or title.lower().startswith(
        "show hypernews:"
    )

    slug = slugify(title) or "post"
    db = get_db()

    async with db.transaction():
        post = Post(
            title=title,
            slug=slug,
            url=url,
            text=text,
            author_id=user.id,
            forum_id=forum.id,
            is_ask=is_ask,
            is_show=is_show,
        )
        await post.save()
        await Forum.objects.filter(id=forum.id).update(post_count=F("post_count") + 1)
    _cache.clear()

    # Run automod rules for new post
    if forum.id:
        await _run_automod(db, forum.id, AutomodTrigger.NEW_POST, post=post, user=user)

    return Response.redirect(f"/post/{post.get_external_id()}")


@app.get("/f/{forum_name}/members")
async def forum_members(request, forum_name: str):
    """Paginated member list for a forum."""
    try:
        access = await resolve_forum(request, forum_name, ForumIntent.READ)
    except HTTPException as exc:
        if exc.status_code == 404:
            return app.render("404.html", build_context(request), status=404)
        raise
    forum = access.forum

    db = get_db()
    cursor_param = request.GET.get("cursor", "")

    keyset_where = ""
    params: list = [forum.id]
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None
    if cursor_data and cursor_data[0] == "next":
        keyset_where = " AND fm.id < $2"
        params.append(int(cursor_data[1]))
    limit_idx = len(params) + 1
    params.append(POSTS_PER_PAGE + 1)

    rows = await db.query_tuples(
        f"""SELECT fm.id, fm.user_id, fm.role, fm.joined_at, u.username
            FROM hn_forum_members fm JOIN hn_users u ON u.id = fm.user_id
            WHERE fm.forum_id = $1{keyset_where}
            ORDER BY fm.id DESC LIMIT ${limit_idx}""",
        *params,
    )

    has_more = len(rows) > POSTS_PER_PAGE
    rows = rows[:POSTS_PER_PAGE]

    member_list = [
        {"id": r[0], "username": r[4], "role": r[2], "joined": time_ago(r[3])}
        for r in rows
    ]

    next_cursor = ""
    if has_more and rows:
        next_cursor = _encode_cursor("next", str(rows[-1][0]))

    ctx = await build_forum_context(
        request, forum, members=member_list, next_cursor=next_cursor
    )
    return app.render("forum_members.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Forum moderation
# ---------------------------------------------------------------------------


@app.get("/f/{forum_name}/mod")
@guard(
    *REQUIRE_LOGIN,
    Require.resource(
        "access", resolver=_resolve_forum_moderate, from_path="forum_name"
    ),
)
async def mod_dashboard(request, forum_name: str):
    """Moderator dashboard — stats, recent actions, quick links."""
    access = request.guard.access
    forum = access.forum
    db = get_db()

    # Stats: member count, post count, recent reports, active automod rules
    member_count = forum.subscriber_count
    post_count = forum.post_count

    report_rows = await db.query_tuples(
        """SELECT COUNT(*) FROM hn_spam_reports sr
           JOIN hn_posts p ON p.id = sr.post_id
           WHERE p.forum_id = $1 AND sr.status = 'pending'""",
        forum.id,
    )
    recent_reports = report_rows[0][0] if report_rows else 0

    automod_rows = await db.query_tuples(
        "SELECT COUNT(*) FROM hn_automod_rules WHERE forum_id = $1 AND is_active",
        forum.id,
    )
    active_automod_rules = automod_rows[0][0] if automod_rows else 0

    # Last 10 mod actions
    action_rows = await db.query_tuples(
        """SELECT ma.id, ma.action, ma.reason, ma.created_at, u.username
           FROM hn_mod_actions ma
           JOIN hn_users u ON u.id = ma.moderator_id
           WHERE ma.forum_id = $1
           ORDER BY ma.created_at DESC LIMIT 10""",
        forum.id,
    )
    recent_actions = [
        {
            "id": r[0],
            "action": r[1],
            "reason": r[2],
            "time_ago": time_ago(r[3]),
            "moderator": r[4],
        }
        for r in action_rows
    ]

    ctx = build_context(
        request,
        forum=await _forum_to_dict(forum),
        stats={
            "member_count": member_count,
            "post_count": post_count,
            "recent_reports": recent_reports,
            "active_automod_rules": active_automod_rules,
        },
        recent_actions=recent_actions,
    )
    return app.render("mod_panel.html", ctx)


@app.post("/f/{forum_name}/mod/appoint")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_appoint_mod(request, forum_name: str):
    """Appoint a moderator (forum admin only)."""
    access = request.guard.access
    forum = access.forum

    try:
        data = await validate_form(request, ModAppointSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    target = await User.objects.filter(username=data.username).first()
    if not target:
        raise HTTPException(404, "User not found")

    uid = get_uid(request)
    db = get_db()
    target_member = await ForumMember.objects.filter(
        forum_id=forum.id, user_id=target.id
    ).first()
    if target_member:
        await ForumMember.objects.filter(id=target_member.id).update(
            role=ForumRole.MODERATOR.value
        )
    else:
        async with db.transaction():
            await ForumMember(
                forum_id=forum.id, user_id=target.id, role=ForumRole.MODERATOR
            ).save()
            await Forum.objects.filter(id=forum.id).update(
                subscriber_count=F("subscriber_count") + 1
            )
    await _log_forum_action(uid, forum.id, "appoint_mod", target_user_id=target.id)
    return Response.redirect(f"/f/{forum_name}/about")


@app.post("/f/{forum_name}/mod/remove")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_remove_mod(request, forum_name: str):
    """Remove a moderator (forum admin only). Demotes to subscriber."""
    access = request.guard.access
    forum = access.forum

    try:
        data = await validate_form(request, ModAppointSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    target = await User.objects.filter(username=data.username).first()
    if not target:
        raise HTTPException(404, "User not found")

    uid = get_uid(request)
    target_member = await ForumMember.objects.filter(
        forum_id=forum.id, user_id=target.id
    ).first()
    if not target_member:
        raise HTTPException(404, "User is not a member of this forum")

    # Check if target is the last admin — prevent orphaning the forum
    if _normalize_role(target_member.role) == ForumRole.ADMIN.value:
        admin_count = await ForumMember.objects.filter(
            forum_id=forum.id, role=ForumRole.ADMIN.value
        ).count()
        if admin_count <= 1:
            raise HTTPException(400, "Cannot remove the last admin of a forum")

    await ForumMember.objects.filter(id=target_member.id).update(
        role=ForumRole.SUBSCRIBER.value
    )
    await _log_forum_action(uid, forum.id, "remove_mod", target_user_id=target.id)
    return Response.redirect(f"/f/{forum_name}/about")


@app.post("/f/{forum_name}/mod/transfer")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_transfer_admin(request, forum_name: str):
    """Transfer forum admin role to another user. Current admin becomes moderator."""
    access = request.guard.access
    forum = access.forum

    try:
        data = await validate_form(request, ModAppointSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    target = await User.objects.filter(username=data.username).first()
    if not target:
        raise HTTPException(404, "User not found")

    uid = get_uid(request)
    if target.id == uid:
        raise HTTPException(400, "Cannot transfer to yourself")

    db = get_db()
    async with db.transaction():
        # Promote target to admin
        target_member = await ForumMember.objects.filter(
            forum_id=forum.id, user_id=target.id
        ).first()
        if target_member:
            await ForumMember.objects.filter(id=target_member.id).update(
                role=ForumRole.ADMIN.value
            )
        else:
            await ForumMember(
                forum_id=forum.id, user_id=target.id, role=ForumRole.ADMIN
            ).save()
            await Forum.objects.filter(id=forum.id).update(
                subscriber_count=F("subscriber_count") + 1
            )
        # Demote caller to moderator
        await ForumMember.objects.filter(forum_id=forum.id, user_id=uid).update(
            role=ForumRole.MODERATOR.value
        )
    await _log_forum_action(
        uid,
        forum.id,
        "transfer_admin",
        target_user_id=target.id,
        reason=f"admin transferred from user {uid} to user {target.id}",
    )
    _cache.clear()
    return Response.redirect(f"/f/{forum_name}/about")


@app.post("/f/{forum_name}/delete")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_delete(request, forum_name: str):
    """Delete a forum (site staff only). Soft-deletes by hiding + archiving.

    Posts remain accessible via direct links but forum is removed from directory.
    Fully destructive deletion requires database-level access.
    """
    access = request.guard.access
    forum = access.forum
    # Only site staff can delete forums (not just forum admins).
    # Use the live DB user (request.guard.active_user) rather than session dict
    # to catch mid-session staff revocations.
    if not await request.guard.active_user.has_status("access", "staff"):
        raise HTTPException(403, "Only site staff can delete forums")

    uid = get_uid(request)
    await forum.set_status(
        "archive", "archived", reason="Soft-deleted by staff", actor_id=uid
    )
    await forum.set_status(
        "lock", "locked", reason="Soft-deleted by staff", actor_id=uid
    )
    await forum.set_status(
        "visibility", "hidden", reason="Soft-deleted by staff", actor_id=uid
    )
    await _log_forum_action(
        uid,
        forum.id,
        "forum_delete",
        reason=f"forum '{forum_name}' soft-deleted by staff",
    )
    _cache.clear()
    return Response.redirect("/forums")


async def _fetch_audit_log_data(forum: Forum, db) -> list[dict[str, str | int]]:
    """Fetch audit log data for a forum. Shared by JSON and HTML routes."""
    rows = await db.query_tuples(
        """SELECT ma.id, ma.moderator_id, ma.action, ma.reason,
                  ma.target_user_id, ma.created_at, u.username AS mod_name,
                  COALESCE(t.username, '') AS target_name
           FROM hn_mod_actions ma
           JOIN hn_users u ON u.id = ma.moderator_id
           LEFT JOIN hn_users t ON t.id = ma.target_user_id AND ma.target_user_id > 0
           WHERE ma.forum_id = $1
           ORDER BY ma.created_at DESC
           LIMIT 100""",
        forum.id,
    )
    return [
        {
            "id": r[0],
            "moderator": r[6],
            "action": r[2],
            "reason": r[3],
            "target_user": r[7] if r[4] else "",
            "time_ago": time_ago(r[5]),
        }
        for r in rows
    ]


@app.get("/f/{forum_name}/audit")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_audit_log(request, forum_name: str):
    """View audit log of all moderation actions on this forum. Admin/staff only."""
    access = request.guard.access
    actions = await _fetch_audit_log_data(access.forum, get_db())
    return Response.json({"actions": actions})


@app.get("/f/{forum_name}/audit/view")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def forum_audit_log_view(request, forum_name: str):
    """Render HTML audit log page for a forum. Admin/staff only."""
    access = request.guard.access
    actions = await _fetch_audit_log_data(access.forum, get_db())
    ctx = build_context(
        request, forum=await _forum_to_dict(access.forum), actions=actions
    )
    return app.render("mod_audit_log.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Forum search
# ---------------------------------------------------------------------------


@app.get("/forums/search")
async def forum_search(request):
    """Full-text search across forum names and descriptions.

    Includes private forums the current user is a member of.
    Hidden forums are excluded unless user is a member.
    """
    query = (request.GET.get("q") or "").strip()
    if not query:
        return Response.redirect("/forums")

    db = get_db()
    uid = get_uid_or_none(request)

    # Visibility: public non-hidden, OR user is a member (even if hidden/private)
    # Pre-compute visible forum IDs set to keep SQL clean
    visible = await _visible_forum_ids(uid)
    visible_list = [fid for fid in visible if fid > 0]  # exclude 0 (not a forum)
    if visible_list:
        visibility = "id = ANY($1::int[])"
    else:
        visibility = """is_public = true AND NOT EXISTS (
            SELECT 1 FROM hyper_status_events e
            WHERE e.entity_type = 'forum' AND e.entity_id = hn_forums.id
            AND e.status = 'hidden' AND e.ended_at IS NULL
            AND (e.expires_at IS NULL OR e.expires_at > now())
        )"""

    if visible_list:
        rows = await db.query_tuples(
            f"""SELECT id, name, title, description, subscriber_count, post_count, created_at
               FROM hn_forums WHERE {visibility}
                 AND (to_tsvector('english', name || ' ' || title || ' ' || COALESCE(description, ''))
                      @@ plainto_tsquery('english', $2))
               ORDER BY subscriber_count DESC
               LIMIT $3""",
            visible_list,
            query,
            FORUMS_PER_PAGE,
        )
    else:
        rows = await db.query_tuples(
            f"""SELECT id, name, title, description, subscriber_count, post_count, created_at
               FROM hn_forums WHERE {visibility}
                 AND (to_tsvector('english', name || ' ' || title || ' ' || COALESCE(description, ''))
                      @@ plainto_tsquery('english', $1))
               ORDER BY subscriber_count DESC
               LIMIT $2""",
            query,
            FORUMS_PER_PAGE,
        )

    forum_list = []
    for r in rows:
        forum_list.append(
            {
                "id": r[0],
                "name": r[1],
                "title": r[2],
                "description": r[3][:200] if r[3] else "",
                "subscriber_count": r[4],
                "post_count": r[5],
                "time_ago": time_ago(r[6]),
            }
        )

    ctx = build_context(request, forums=forum_list, query=query, sort="search")
    return app.render("forums.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Search (PostgreSQL full-text search)
# ---------------------------------------------------------------------------


@app.get("/search")
async def search(request):
    """Full-text search across post titles and text using PostgreSQL tsvector."""
    query = (request.GET.get("q") or "").strip()
    cursor_param = request.GET.get("cursor", "")
    per_page = POSTS_PER_PAGE

    if not query:
        return app.render(
            "search.html",
            build_context(
                request,
                query="",
                posts=[],
                total=0,
                next_cursor="",
                prev_cursor="",
            ),
        )

    db = get_db()
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None

    # Private forum filter — only show posts from visible forums
    uid = get_uid_or_none(request)
    visible = await _visible_forum_ids(uid)
    visible_list = list(visible)

    # Use plainto_tsquery for safe user input (no special operators needed)
    total = await db.query_val(
        """SELECT count(*) FROM hn_posts
           WHERE is_deleted = false
             AND forum_id = ANY($2::int[])
             AND (to_tsvector('english', title) || to_tsvector('english', text)) @@ plainto_tsquery('english', $1)""",
        query,
        visible_list,
    )

    # Keyset pagination on (rank DESC, score DESC, id DESC)
    if cursor_data and cursor_data[0] == "next":
        parts = str(cursor_data[1]).split("|", 2)
        if len(parts) == 3:
            rows = await db.query_tuples(
                """SELECT id, title, slug, url, text, author_id, score, comment_count, is_ask, created_at,
                          ts_rank(to_tsvector('english', title) || to_tsvector('english', text),
                                  plainto_tsquery('english', $1)) AS rank
                   FROM hn_posts
                   WHERE is_deleted = false
                     AND forum_id = ANY($2::int[])
                     AND (to_tsvector('english', title) || to_tsvector('english', text)) @@ plainto_tsquery('english', $1)
                     AND (ts_rank(to_tsvector('english', title) || to_tsvector('english', text),
                                  plainto_tsquery('english', $1)), score, id) < ($3::float8, $4::int, $5::int)
                   ORDER BY rank DESC, score DESC, id DESC
                   LIMIT $6""",
                query,
                visible_list,
                float(parts[0]),
                int(parts[1]),
                int(parts[2]),
                per_page + 1,
            )
        else:
            rows = await db.query_tuples(
                """SELECT id, title, slug, url, text, author_id, score, comment_count, is_ask, created_at,
                          ts_rank(to_tsvector('english', title) || to_tsvector('english', text),
                                  plainto_tsquery('english', $1)) AS rank
                   FROM hn_posts
                   WHERE is_deleted = false
                     AND forum_id = ANY($2::int[])
                     AND (to_tsvector('english', title) || to_tsvector('english', text)) @@ plainto_tsquery('english', $1)
                   ORDER BY rank DESC, score DESC, id DESC
                   LIMIT $3""",
                query,
                visible_list,
                per_page + 1,
            )
    else:
        rows = await db.query_tuples(
            """SELECT id, title, slug, url, text, author_id, score, comment_count, is_ask, created_at,
                      ts_rank(to_tsvector('english', title) || to_tsvector('english', text),
                              plainto_tsquery('english', $1)) AS rank
               FROM hn_posts
               WHERE is_deleted = false
                 AND forum_id = ANY($2::int[])
                 AND (to_tsvector('english', title) || to_tsvector('english', text)) @@ plainto_tsquery('english', $1)
               ORDER BY rank DESC, score DESC, id DESC
               LIMIT $3""",
            query,
            visible_list,
            per_page + 1,
        )

    has_more = len(rows) > per_page
    rows = rows[:per_page]

    # Batch fetch authors
    author_ids = list({r[5] for r in rows})
    authors: dict[int, str] = {}
    if author_ids:
        author_rows = await User.objects.filter(id__in=author_ids).all()
        authors = {u.id: u.username for u in author_rows}

    # Build post list — need Post instances for get_external_id()
    post_list = []
    for i, r in enumerate(rows):
        post_obj = Post(id=r[0])
        post_list.append(
            {
                "rank": i + 1,
                "id": r[0],
                "pid": post_obj.get_external_id(),
                "title": r[1],
                "slug": r[2],
                "url": r[3],
                "domain": domain_from_url(r[3]),
                "score": r[6],
                "author": authors.get(r[5], "unknown"),
                "comment_count": r[7],
                "time_ago": time_ago(r[9]),
                "is_ask": r[8],
            }
        )

    # Cursor generation — rank is at index 10, score at 6, id at 0
    next_cursor = ""
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor("next", f"{last[10]}|{last[6]}|{last[0]}")
    prev_cursor = ""
    if cursor_param and rows:
        first = rows[0]
        prev_cursor = _encode_cursor("prev", f"{first[10]}|{first[6]}|{first[0]}")

    ctx = build_context(
        request,
        query=query,
        posts=post_list,
        total=total,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
    return app.render("search.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Post detail
# ---------------------------------------------------------------------------


@app.get("/post/{pid}")
async def post_detail(request, pid):
    """Post detail with threaded comments."""
    access = await resolve_post(request, pid)
    post = access.post

    comments_raw = (
        await Comment.objects.filter(post_id=post.id, is_deleted=False)
        .order_by("created_at")
        .all()
    )

    # Batch fetch ALL authors (post author + comment authors) — single IN query
    all_author_ids = list({post.author_id} | {c.author_id for c in comments_raw})
    author_rows = await User.objects.filter(id__in=all_author_ids).all()
    author_map: dict[int, str] = {u.id: u.username for u in author_rows}

    author = author_map.get(post.author_id, "deleted")

    # Build threaded comment tree
    comment_map = {}
    for c in comments_raw:
        comment_map[c.id] = {
            "id": c.id,
            "text": c.text,
            "author": author_map.get(c.author_id, "deleted"),
            "score": c.score,
            "depth": c.depth,
            "parent_id": c.parent_id,
            "time_ago": time_ago(c.created_at),
            "children": [],
        }

    root_comments = []
    for c in comments_raw:
        cd = comment_map[c.id]
        if c.parent_id and c.parent_id in comment_map:
            comment_map[c.parent_id]["children"].append(cd)
        else:
            root_comments.append(cd)

    # Check if current user has voted — single query with LIMIT 1
    user_vote = None
    if request.user:
        uid = get_uid_or_none(request)
        if uid:
            existing = await Vote.objects.filter(user_id=uid, post_id=post.id).first()
            if existing:
                user_vote = existing.value

    # Reuse forum from resolve_post
    forum_info = None
    if access.forum:
        forum_info = {"name": access.forum.name, "title": access.forum.title}

    ctx = build_context(
        request,
        post={
            "id": post.id,
            "pid": post.get_external_id(),
            "title": post.title,
            "url": post.url,
            "domain": domain_from_url(post.url),
            "text": post.text,
            "score": post.score,
            "author": author,
            "comment_count": post.comment_count,
            "time_ago": time_ago(post.created_at),
            "is_ask": post.is_ask,
            "forum": forum_info,
        },
        comments=root_comments,
        user_vote=user_vote,
    )
    return app.render("post_detail.html", ctx)


# ---------------------------------------------------------------------------
# Helpers: active public forums (for submit dropdowns)
# ---------------------------------------------------------------------------


async def _active_public_forums() -> list[Forum]:
    """Return public forums that are not archived (via timeline status)."""
    forums = await Forum.objects.filter(is_public=True).order_by("name").all()
    result: list[Forum] = []
    for f in forums:
        if not await f.has_status("archive", "archived"):
            result.append(f)
    return result


# ---------------------------------------------------------------------------
# Routes: Submit post
# ---------------------------------------------------------------------------


@app.get("/submit")
@guard(*REQUIRE_LOGIN)
async def submit_form(request):
    """Show post submission form with forum picker."""
    # Fetch all public forums for the dropdown
    forums = await _active_public_forums()
    forum_list = [{"id": f.id, "name": f.name, "title": f.title} for f in forums]
    preselect = request.GET.get("forum", "")
    ctx = build_context(request, forums=forum_list, preselect_forum=preselect)
    return app.render("submit.html", ctx)


@app.post("/submit")
@guard(*REQUIRE_ACTIVE)
async def submit_post(request):
    """Handle post submission with forum selection."""
    check_action_rate_limit(request.guard.active_user.id, "submit", 3, 60)

    try:
        data = await validate_form(request, SubmitPostSchema)
    except ValidationErrors as exc:
        forums = await _active_public_forums()
        forum_list = [{"id": f.id, "name": f.name, "title": f.title} for f in forums]
        ctx = build_context(
            request, errors=[str(e) for e in exc.errors], forums=forum_list
        )
        return app.render("submit.html", ctx, status=400)

    title = data.title
    url = data.url
    text = data.text
    forum_name = data.forum

    # Cross-field check: url or text required
    errors = []
    if not url and not text:
        errors.append("Either URL or text is required")

    # Validate forum via purpose-based resolver (checks archived, locked, private)
    forum_id = 0
    if forum_name:
        try:
            forum_access = await resolve_forum(
                request, forum_name, ForumIntent.WRITE_POST
            )
            forum_id = forum_access.forum.id
        except HTTPException as exc:
            errors.append(exc.detail)

    if errors:
        forums = await _active_public_forums()
        forum_list = [{"id": f.id, "name": f.name, "title": f.title} for f in forums]
        ctx = build_context(
            request,
            errors=errors,
            title=title,
            url=url,
            text=text,
            forums=forum_list,
            preselect_forum=forum_name,
        )
        return app.render("submit.html", ctx, status=400)

    # ban/mute already enforced by @guard(*REQUIRE_ACTIVE)
    is_ask = title.lower().startswith("ask hn:") or title.lower().startswith(
        "ask hypernews:"
    )
    is_show = title.lower().startswith("show hn:") or title.lower().startswith(
        "show hypernews:"
    )

    slug = slugify(title) or "post"
    uid = request.guard.active_user.id
    db = get_db()

    async with db.transaction():
        post = Post(
            title=title,
            slug=slug,
            url=url,
            text=text,
            author_id=uid,
            forum_id=forum_id,
            is_ask=is_ask,
            is_show=is_show,
        )
        await post.save()
        if forum_id:
            await Forum.objects.filter(id=forum_id).update(
                post_count=F("post_count") + 1
            )
    _cache.clear()

    return Response.redirect(f"/post/{post.get_external_id()}")


# ---------------------------------------------------------------------------
# Routes: Voting (HTMX)
# ---------------------------------------------------------------------------


@app.post("/vote")
@guard(*REQUIRE_ACTIVE)
async def vote(request):
    """Handle upvote/downvote via HTMX.

    Hardened: advisory lock (race-safe), ban/mute check, per-user rate limit,
    self-vote prevention, rapid-fire detection.
    """
    try:
        data = await validate_form(request, VoteSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id
    value = 1 if data.direction == "up" else -1

    user = request.guard.active_user
    uid = user.id
    db = get_db()

    # Verify forum access for the target post/comment
    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )

    # Per-user vote rate limit (30/min)
    check_vote_rate_limit(uid)

    # Self-vote prevention
    await check_self_vote(db, uid, post_id, comment_id)

    # Vote weight from trust tier (1.0–2.0 based on karma, 3.0 for staff)
    weight = await get_vote_weight(user)

    # Rapid-fire detection (logs to SecurityLog if >10 in 10s)
    await log_rapid_fire(db, uid)

    # Read existing vote OUTSIDE transaction (for permission check)
    if post_id:
        old_vote = await Vote.objects.filter(user_id=uid, post_id=post_id).first()
    else:
        old_vote = await Vote.objects.filter(user_id=uid, comment_id=comment_id).first()

    # Downvote permission check BEFORE transaction (avoids poisoning prep cache on rollback)
    # Toggle-off is always allowed. Only check for new downvotes or flips to downvote.
    if value == -1 and (not old_vote or old_vote.value != value):
        await check_downvote_permission(user, value)

    # Advisory lock prevents race condition on concurrent votes for same target.
    lock_key = hash((uid, post_id, comment_id)) & 0x7FFFFFFF
    async with db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

        # Re-read inside lock (may have changed between read and lock acquisition)
        if post_id:
            old_vote = await Vote.objects.filter(user_id=uid, post_id=post_id).first()
        else:
            old_vote = await Vote.objects.filter(
                user_id=uid, comment_id=comment_id
            ).first()

        if old_vote:
            if old_vote.value == value:
                # Toggle off — ALWAYS allowed regardless of karma
                await Vote.objects.filter(id=old_vote.id).delete()
                delta = -value
            else:
                # Flip direction
                await Vote.objects.filter(id=old_vote.id).update(value=value)
                delta = value * 2
        else:
            # New vote
            v = Vote(user_id=uid, post_id=post_id, comment_id=comment_id, value=value)
            await v.save()
            delta = value

        # Compute upvote/downvote deltas from the vote change
        # delta > 0 means net upvote added, delta < 0 means net downvote added
        # For new vote: delta=+1 (upvote) or delta=-1 (downvote)
        # For flip: delta=+2 (down→up) or delta=-2 (up→down)
        # For toggle off: delta=-1 (removed upvote) or delta=+1 (removed downvote)
        up_delta = max(delta, 0)  # positive part
        down_delta = max(-delta, 0)  # negative part (as positive count)
        if old_vote and old_vote.value == value:
            # Toggle off — reverse the original vote's contribution
            if value == 1:
                up_delta, down_delta = -1, 0
            else:
                up_delta, down_delta = 0, -1
        elif old_vote:
            # Flip — remove old, add new
            if value == 1:
                up_delta, down_delta = 1, -1  # gained upvote, lost downvote
            else:
                up_delta, down_delta = -1, 1  # lost upvote, gained downvote
        else:
            # New vote
            if value == 1:
                up_delta, down_delta = 1, 0
            else:
                up_delta, down_delta = 0, 1

        # Weighted delta: score uses ±1, weighted_score uses ±weight
        weighted_delta = delta * weight

        # Atomic score + weighted_score + upvotes/downvotes + karma update
        # Uses UPDATE...RETURNING to get new score + author_id in one roundtrip
        target_author = 0
        if post_id:
            rows = await Post.objects.filter(id=post_id).update(
                score=F("score") + delta,
                weighted_score=F("weighted_score") + weighted_delta,
                upvotes=F("upvotes") + up_delta,
                downvotes=F("downvotes") + down_delta,
                returning=["score", "author_id"],
            )
            if rows:
                new_score = rows[0]["score"]
                target_author = rows[0]["author_id"]
                if target_author != uid:
                    await User.objects.filter(id=target_author).update(
                        karma=F("karma") + delta
                    )
            else:
                new_score = delta
        elif comment_id:
            rows = await Comment.objects.filter(id=comment_id).update(
                score=F("score") + delta,
                weighted_score=F("weighted_score") + weighted_delta,
                upvotes=F("upvotes") + up_delta,
                downvotes=F("downvotes") + down_delta,
                returning=["score", "author_id"],
            )
            if rows:
                new_score = rows[0]["score"]
                target_author = rows[0]["author_id"]
                if target_author != uid:
                    await User.objects.filter(id=target_author).update(
                        karma=F("karma") + delta
                    )
            else:
                new_score = delta
        else:
            raise HTTPException(400, "Must specify post_id or comment_id")

    # After transaction commits: record analytics event + invalidate cache
    domain = ""
    if post_id:
        url_row = await db.query_tuples(
            "SELECT url FROM hn_posts WHERE id = $1", post_id
        )
        if url_row and url_row[0][0]:
            domain = domain_from_url(url_row[0][0])
    await record_vote_event(
        db,
        uid,
        TargetType.POST if post_id else TargetType.COMMENT,
        post_id or comment_id,
        target_author,
        value,
        weight,
        user.karma,
        domain,
    )
    _cache.clear()

    if post_id:
        return Response.html(
            f'<span class="score" id="score-post-{post_id}">{new_score} point{"s" if new_score != 1 else ""}</span>'
        )
    return Response.html(
        f'<span class="score" id="score-comment-{comment_id}">{new_score} point{"s" if new_score != 1 else ""}</span>'
    )


# ---------------------------------------------------------------------------
# Routes: Comments
# ---------------------------------------------------------------------------


@app.post("/comment")
@guard(*REQUIRE_ACTIVE)
async def add_comment(request):
    """Add a comment to a post (or reply to a comment)."""
    try:
        data = await validate_form(request, CommentSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    parent_id = data.parent_id
    text = data.text

    user = request.guard.active_user
    uid = user.id
    db = get_db()
    target_forum_id = await require_target_forum_access(request, db, post_id=post_id)
    check_action_rate_limit(uid, "comment", 10, 60)

    depth = 0
    if parent_id:
        parent = await Comment.objects.filter(id=parent_id).first()
        if parent:
            depth = parent.depth + 1
            if depth > MAX_COMMENT_DEPTH:
                raise HTTPException(
                    400, f"Reply too deeply nested (max {MAX_COMMENT_DEPTH} levels)"
                )

    comment = Comment(
        post_id=post_id,
        author_id=uid,
        parent_id=parent_id,
        depth=depth,
        text=text,
    )

    # Transaction: save comment + increment count atomically
    async with db.transaction():
        await comment.save()
        await Post.objects.filter(id=post_id).update(
            comment_count=F("comment_count") + 1
        )
    _cache.clear()

    # Run automod for new comment (use forum_id from access check — no re-fetch needed)
    if target_forum_id:
        await _run_automod(
            db, target_forum_id, AutomodTrigger.NEW_COMMENT, comment=comment, user=user
        )

    # Notify parent comment author (reply notification) — single JOIN query for author + post title
    if parent_id:
        parent_row = await db.query_tuples(
            """SELECT c.author_id, p.title
               FROM hn_comments c JOIN hn_posts p ON p.id = c.post_id
               WHERE c.id = $1""",
            parent_id,
        )
        if parent_row:
            parent_author_id, post_title = parent_row[0][0], parent_row[0][1][:80]
            await _notify(
                parent_author_id,
                NotificationType.REPLY,
                f'{user.username} replied to your comment on "{post_title}"',
                actor_id=uid,
                post_id=post_id,
                comment_id=comment.id,
            )

    # Notify @mentioned users — batch lookup
    mention_names = _extract_mentions(text)
    if mention_names:
        mentioned_users = await User.objects.filter(username__in=mention_names).all()
        for mentioned in mentioned_users:
            await _notify(
                mentioned.id,
                NotificationType.MENTION,
                f"{user.username} mentioned you in a comment",
                actor_id=uid,
                post_id=post_id,
                comment_id=comment.id,
            )

    # If HTMX request, return just the new comment partial
    if request.headers.get("hx-request"):
        author_name = _html.escape(user.username) if user else "anonymous"
        safe_text = _html.escape(text)
        html = f"""
        <div class="comment" style="margin-left: {depth * 20}px">
            <div class="comment-meta">
                <a href="/user/{author_name}" class="comment-author">{author_name}</a>
                <span class="comment-time">just now</span>
            </div>
            <div class="comment-text">{safe_text}</div>
            <div class="comment-actions">
                <a href="#" class="reply-link" hx-get="/reply-form?post_id={post_id}&parent_id={comment.id}" hx-target="#reply-{comment.id}" hx-swap="innerHTML">reply</a>
            </div>
            <div id="reply-{comment.id}"></div>
        </div>
        """
        return Response.html(html)

    return Response.redirect(f"/post/{Post._id_manager.encode(post_id)}")


@app.get("/reply-form")
@guard(*REQUIRE_LOGIN)
async def reply_form(request):
    """Return a reply form partial (HTMX)."""
    post_id = _html.escape(request.GET.get("post_id", "0"))
    parent_id = _html.escape(request.GET.get("parent_id", "0"))
    csrf_token = _html.escape(request.cookies.get("csrftoken", ""))
    html = f"""
    <form class="reply-form" hx-post="/comment" hx-target="closest .comment" hx-swap="afterend">
        <input type="hidden" name="_csrf_token" value="{csrf_token}">
        <input type="hidden" name="post_id" value="{post_id}">
        <input type="hidden" name="parent_id" value="{parent_id}">
        <textarea name="text" rows="4" placeholder="Write a reply..." required></textarea>
        <button type="submit">Reply</button>
        <button type="button" onclick="this.closest('.reply-form').remove()">Cancel</button>
    </form>
    """
    return Response.html(html)


# ---------------------------------------------------------------------------
# Routes: User profile
# ---------------------------------------------------------------------------


@app.get("/user/{username}")
async def user_profile(request, username):
    """Public user profile page — lookup by unique username.

    Five sequential data queries after resolving the user. ``asyncio.gather``
    parallelization was tested under task #170 with pool sizes 16-64 and
    showed no benefit on this endpoint: each query is ~100 μs (small) and
    the asyncio task-creation overhead per gather entry (~100 μs × 5 = 500 μs)
    exceeds the saved sequential DB wait (~400 μs). Parallelization wins
    when query latency dominates scheduling cost — not the case here.

    SQL is encapsulated as model classmethods (``Comment.recent_by_author_in_forums``,
    ``ForumMember.public_memberships_for_user``) so the handler stays
    presentation-only and the queries are reusable from other endpoints.
    """
    profile_user = await User.objects.filter(username=username).first()
    if not profile_user:
        return app.render("404.html", build_context(request), status=404)

    # Filter by visible forums to prevent private forum content leaking
    viewer_uid = get_uid_or_none(request)
    visible = await _visible_forum_ids(viewer_uid)
    visible_list = list(visible)
    user_id = profile_user.id

    recent_posts = (
        await Post.objects.filter(
            author_id=user_id,
            is_deleted=False,
            forum_id__in=visible_list,
        )
        .order_by("-created_at")
        .limit(20)
        .all()
    )
    recent_comments_rows = await Comment.recent_by_author_in_forums(
        user_id, visible_list
    )
    ext_profile = await UserProfile.objects.filter(user_id=user_id).first()
    forum_memberships = await ForumMember.public_memberships_for_user(user_id)
    is_staff = await profile_user.has_status("access", "staff")

    post_list = [
        {
            "id": p.id,
            "pid": p.get_external_id(),
            "title": p.title,
            "score": p.score,
            "comment_count": p.comment_count,
            "time_ago": time_ago(p.created_at),
        }
        for p in recent_posts
    ]

    comment_list = [
        {
            "id": r[0],
            "post_pid": Post._id_manager.encode(r[1]),
            "text": r[2][:200] if r[2] else "",
            "score": r[3],
            "time_ago": time_ago(r[4]),
        }
        for r in recent_comments_rows
    ]

    forums_list = [
        {"name": r[0], "title": r[1], "role": r[2]} for r in forum_memberships
    ]

    ctx = build_context(
        request,
        profile={
            "id": profile_user.id,
            "username": profile_user.username,
            "display_name": profile_user.display_name,
            "bio": profile_user.bio,
            "karma": profile_user.karma,
            "created_at": time_ago(profile_user.created_at),
            "has_staff_status": is_staff,
            "website": ext_profile.website if ext_profile else "",
            "location": ext_profile.location if ext_profile else "",
            "avatar_url": ext_profile.avatar_url if ext_profile else "",
            "github_username": ext_profile.github_username if ext_profile else "",
        },
        recent_posts=post_list,
        recent_comments=comment_list,
        forums=forums_list,
    )
    return app.render("user_profile.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Account settings
# ---------------------------------------------------------------------------


@app.get("/account")
@guard(*REQUIRE_LOGIN)
async def account_page(request):
    """Account settings page."""
    user = await get_current_user(request)
    if not user:
        return Response.redirect("/login")
    ctx = build_context(
        request,
        account={
            "display_name": user.display_name,
            "bio": user.bio,
            "email": user.email,
        },
    )
    return app.render("account.html", ctx)


@app.post("/account")
@guard(*REQUIRE_ACTIVE)
async def update_account(request):
    """Handle account settings update."""
    user = request.guard.active_user

    try:
        data = await validate_form(request, AccountSchema)
    except ValidationErrors as exc:
        ctx = build_context(
            request,
            account={
                "display_name": user.display_name,
                "bio": user.bio,
                "email": user.email,
            },
            errors=[str(e) for e in exc.errors],
        )
        return app.render("account.html", ctx, status=400)

    display_name = data.display_name
    bio = data.bio
    email = data.email

    await User.objects.filter(id=user.id).update(
        display_name=display_name,
        bio=bio,
        email=email,
    )

    # Handle password change if provided
    if data.new_password:
        if not verify_password(data.current_password, user.password_hash):
            ctx = build_context(
                request,
                account={"display_name": display_name, "bio": bio, "email": email},
                error="Current password is incorrect",
            )
            return app.render("account.html", ctx, status=400)
        new_hash = hash_password(data.new_password)
        await User.objects.filter(id=user.id).update(password_hash=new_hash)

    ctx = build_context(
        request,
        account={"display_name": display_name, "bio": bio, "email": email},
        success="Account updated successfully",
    )
    return app.render("account.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Auth (login, register, logout)
# ---------------------------------------------------------------------------


@app.get("/login")
async def login_page(request):
    """Login form."""
    ctx = build_context(request)
    return app.render("login.html", ctx)


@app.post("/login")
async def login_handler(request):
    """Handle login form submission with brute force protection."""
    try:
        data = await validate_form(request, LoginSchema)
    except ValidationErrors as exc:
        ctx = build_context(
            request, error=str(exc.errors[0]) if exc.errors else "Invalid input"
        )
        return app.render("login.html", ctx, status=400)
    username = data.username
    password = data.password

    # Brute force protection
    client_ip = request.client_ip or "unknown"
    if auth.is_login_blocked(client_ip):
        ctx = build_context(
            request, error="Too many login attempts — please wait a few minutes"
        )
        return app.render("login.html", ctx, status=429)

    user = await User.objects.filter(username=username).first()
    if not user or not verify_password(password, user.password_hash):
        auth.record_failed_login(client_ip)
        ctx = build_context(request, error="Invalid username or password")
        return app.render("login.html", ctx, status=400)

    if await user.has_status("moderation", "banned"):
        ctx = build_context(request, error="Your account has been banned")
        return app.render("login.html", ctx, status=403)

    auth.clear_login_attempts(client_ip)

    # Prevent open redirect — only allow safe relative paths
    next_url = request.GET.get("next", "/")
    if not is_safe_redirect_url(next_url):
        next_url = "/"

    resp = Response.redirect(next_url)
    is_staff = await user.has_status("access", "staff")
    # HyperNews uses hn_users (not hyper_users), so always pass explicit groups
    # to avoid ID collisions with the RBAC hyper_user_groups table.
    groups = ["staff"] if is_staff else []
    session = await build_session_data(
        user.id,
        get_db(),
        groups=groups,
        username=user.username,
    )
    auth.login(resp, session, request)
    return resp


@app.get("/register")
async def register_page(request):
    """Registration form."""
    ctx = build_context(request)
    return app.render("register.html", ctx)


@app.post("/register")
async def register_handler(request):
    """Handle registration form submission."""
    try:
        data = await validate_form(request, RegisterSchema)
    except ValidationErrors as exc:
        ctx = build_context(request, errors=[str(e) for e in exc.errors])
        return app.render("register.html", ctx, status=400)

    username = data.username
    password = data.password
    email = data.email

    # Check for duplicate username. `.exists()` is cheaper than
    # `.first()` since we only care about presence.
    if await User.objects.filter(username=username).exists():
        ctx = build_context(
            request,
            errors=["Registration failed — please try a different username"],
            username=username,
            email=email,
        )
        return app.render("register.html", ctx, status=400)

    pw_hash = hash_password(password)
    user = User(
        username=username,
        email=email,
        password_hash=pw_hash,
    )
    try:
        await user.save()
    except IntegrityError:
        # Race condition: another request created the same username
        ctx = build_context(
            request,
            errors=["Registration failed — please try a different username"],
            username=username,
            email=email,
        )
        return app.render("register.html", ctx, status=400)

    resp = Response.redirect("/")
    session = await build_session_data(
        user.id, get_db(), groups=[], username=user.username
    )
    auth.login(resp, session, request)
    return resp


@app.post("/logout")
async def logout_handler(request):
    """Handle logout via POST (CSRF-protected)."""
    resp = Response.redirect("/")
    if request.session_id:
        auth.logout(resp, request.session_id)
    return resp


# ---------------------------------------------------------------------------
# Routes: Admin messaging
# ---------------------------------------------------------------------------


@app.get("/messages")
@guard(*REQUIRE_LOGIN)
async def messages_inbox(request):
    """View messages inbox."""
    uid = get_uid(request)
    messages = (
        await AdminMessage.objects.filter(to_user_id=uid)
        .order_by("-created_at")
        .limit(50)
        .all()
    )

    # Batch fetch senders — single IN query instead of N+1
    sender_ids = list({m.from_user_id for m in messages})
    sender_map: dict[int, str] = {}
    if sender_ids:
        sender_rows = await User.objects.filter(id__in=sender_ids).all()
        sender_map = {u.id: u.username for u in sender_rows}

    msg_list = []
    for m in messages:
        msg_list.append(
            {
                "id": m.id,
                "from_user": sender_map.get(m.from_user_id, "system"),
                "subject": m.subject,
                "body": m.body,
                "is_read": m.is_read,
                "time_ago": time_ago(m.created_at),
            }
        )

    # Mark all as read
    await AdminMessage.objects.filter(to_user_id=uid, is_read=False).update(
        is_read=True
    )

    ctx = build_context(request, messages=msg_list)
    return app.render("messages.html", ctx)


@app.post("/messages/send")
@guard(*REQUIRE_ACTIVE, Require.staff())
async def send_message(request):
    """Send a message to another user (staff only)."""
    uid = get_uid(request)
    check_action_rate_limit(uid, "message", 10, 60)

    try:
        data = await validate_form(request, MessageSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    to_user = await User.objects.filter(username=data.to_username).first()
    if not to_user:
        raise HTTPException(404, "Recipient not found")

    msg = AdminMessage(
        from_user_id=uid,
        to_user_id=to_user.id,
        subject=data.subject,
        body=data.body,
    )
    await msg.save()
    return Response.redirect("/messages")


# ---------------------------------------------------------------------------
# Routes: Spam reporting
# ---------------------------------------------------------------------------


@app.post("/report")
@guard(*REQUIRE_ACTIVE)
async def report_spam(request):
    """Report a post or comment as spam."""
    user = request.guard.active_user
    check_action_rate_limit(user.id, "report", 10, 60)

    try:
        data = await validate_form(request, ReportSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id
    reason = data.reason

    # Verify forum access
    db = get_db()
    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )

    # Dedup: check if already reported by this user
    existing = await SpamReport.objects.filter(
        reporter_id=user.id,
        post_id=post_id,
        comment_id=comment_id,
    ).first()
    if existing:
        return Response.json({"ok": True, "message": "Already reported"})

    report = SpamReport(
        reporter_id=user.id,
        post_id=post_id,
        comment_id=comment_id,
        reason=reason,
    )
    await report.save()

    if request.headers.get("hx-request"):
        return Response.html('<span class="reported">Reported. Thank you.</span>')
    return Response.redirect(
        f"/post/{Post._id_manager.encode(post_id)}" if post_id else "/"
    )


# ---------------------------------------------------------------------------
# Meta-Moderation (Phase 5)
# ---------------------------------------------------------------------------


@app.post("/agree")
@guard(*REQUIRE_ACTIVE)
async def agree_vote(request):
    """Cast an agree/disagree vote on content (separate from quality votes).

    Hardened: rate limit, self-vote prevention, advisory lock, cache invalidation.
    """
    try:
        data = await validate_form(request, AgreeVoteSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id
    direction = data.direction
    value = 1 if direction == "agree" else -1

    user = request.guard.active_user
    db = get_db()

    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )
    check_vote_rate_limit(user.id)
    await check_self_vote(db, user.id, post_id, comment_id)

    lock_key = hash(("agree", user.id, post_id, comment_id)) & 0x7FFFFFFF
    async with db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

        if post_id:
            old = await AgreementVote.objects.filter(
                user_id=user.id, post_id=post_id
            ).first()
        else:
            old = await AgreementVote.objects.filter(
                user_id=user.id, comment_id=comment_id
            ).first()

        if old:
            if old.value == value:
                await AgreementVote.objects.filter(id=old.id).delete()
            else:
                await AgreementVote.objects.filter(id=old.id).update(value=value)
        else:
            v = AgreementVote(
                user_id=user.id, post_id=post_id, comment_id=comment_id, value=value
            )
            await v.save()

        # Compute deltas
        if old and old.value == value:
            agree_d = -1 if value == 1 else 0
            disagree_d = 0 if value == 1 else -1
        elif old:
            agree_d = 1 if value == 1 else -1
            disagree_d = -1 if value == 1 else 1
        else:
            agree_d = 1 if value == 1 else 0
            disagree_d = 0 if value == 1 else 1

        if post_id:
            await Post.objects.filter(id=post_id).update(
                agree_count=F("agree_count") + agree_d,
                disagree_count=F("disagree_count") + disagree_d,
            )
        elif comment_id:
            await Comment.objects.filter(id=comment_id).update(
                agree_count=F("agree_count") + agree_d,
                disagree_count=F("disagree_count") + disagree_d,
            )

    return Response.json({"ok": True, "direction": direction})


@app.post("/tag")
@guard(*REQUIRE_ACTIVE)
async def tag_content(request):
    """Apply a quality tag to content. Requires 'established' tier (can_flag)."""
    try:
        data = await validate_form(request, TagSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id
    tag = data.tag

    user = request.guard.active_user
    db = get_db()
    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )
    check_action_rate_limit(user.id, "tag", 20, 60)
    await apply_content_tag(db, user, post_id, comment_id, tag)
    return Response.json({"ok": True, "tag": tag})


@app.post("/mod/note")
@guard(*REQUIRE_ACTIVE)
async def add_mod_note(request):
    """Add a moderator note. Staff only."""
    user = request.guard.active_user
    if not await user.has_status("access", "staff"):
        raise HTTPException(403, "Staff only")

    try:
        data = await validate_form(request, ModNoteSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    try:
        visibility = Visibility(data.visibility)
    except ValueError:
        raise HTTPException(400, "Invalid visibility")

    note = ModNote(
        moderator_id=user.id,
        target_user_id=data.target_user_id,
        post_id=data.post_id,
        comment_id=data.comment_id,
        note=data.note,
        visibility=visibility,
    )
    await note.save()
    return Response.json({"ok": True, "id": note.id})


async def _fetch_mod_actions_data(username: str) -> list[dict[str, str | int]]:
    """Fetch mod action history for a user. Shared by JSON and HTML routes."""
    target = await User.objects.filter(username=username).first()
    if not target:
        raise HTTPException(404, "User not found")

    actions = (
        await ModAction.objects.filter(target_user_id=target.id)
        .order_by("-created_at")
        .limit(50)
        .all()
    )
    return [
        {
            "action": a.action,
            "reason": a.reason,
            "post_id": a.post_id,
            "comment_id": a.comment_id,
            "time_ago": time_ago(a.created_at),
        }
        for a in actions
    ]


@app.get("/mod/actions/{username}")
@guard(*REQUIRE_LOGIN, Require.staff())
async def mod_actions_for_user(request, username: str):
    """View moderation action history for a user. Staff only."""
    action_list = await _fetch_mod_actions_data(username)
    return Response.json(action_list)


@app.get("/mod/actions/{username}/view")
@guard(*REQUIRE_LOGIN, Require.staff())
async def mod_actions_for_user_view(request, username: str):
    """Render HTML page of moderation action history for a user. Staff only."""
    action_list = await _fetch_mod_actions_data(username)
    ctx = build_context(request, target_username=username, actions=action_list)
    return app.render("mod_actions.html", ctx)


# ---------------------------------------------------------------------------
# Analytics (staff only)
# ---------------------------------------------------------------------------


@app.get("/analytics/rings")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_rings(request):
    """View detected voting rings. Staff only."""
    db = get_db()
    rings = await detect_voting_rings(db)
    results = [
        {
            "user_a": r[0],
            "user_b": r[1],
            "a_votes_b": r[2],
            "b_votes_a": r[3],
            "reciprocity": round(r[4], 3),
        }
        for r in rings
    ]
    return Response.json(results)


@app.get("/analytics/domains")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_domains(request):
    """View domain authority rankings. Staff only."""
    db = get_db()
    rows = await query_domain_authority(db)
    results = [
        {
            "domain": r[0],
            "total_votes": r[1],
            "weighted_upvotes": round(r[2], 2),
            "avg_voter_karma": round(r[3], 1) if r[3] else 0,
        }
        for r in rows
    ]
    return Response.json(results)


@app.get("/analytics/affinity/{username}")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_affinity(request, username: str):
    """View a user's voting affinity graph. Staff only."""
    target_user = await User.objects.filter(username=username).first()
    if not target_user:
        raise HTTPException(404, "User not found")
    db = get_db()
    rows = await query_user_affinity(db, target_user.id)
    # Resolve author IDs to usernames
    author_ids = [r[0] for r in rows]
    authors: dict[int, str] = {}
    if author_ids:
        author_rows = await User.objects.filter(id__in=author_ids).all()
        authors = {u.id: u.username for u in author_rows}
    results = [
        {
            "author": authors.get(r[0], f"user_{r[0]}"),
            "vote_count": r[1],
            "net_sentiment": r[2],
            "avg_weight": round(r[3], 2),
        }
        for r in rows
    ]
    return Response.json(results)


@app.get("/analytics/centrality")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_centrality(request):
    """View users with highest eigenvector centrality. Staff only."""
    db = get_db()
    rows = await query_centrality_leaders(db)
    return Response.json(
        [{"username": r[1], "centrality": round(r[2], 4), "karma": r[3]} for r in rows]
    )


@app.get("/analytics/communities")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_communities(request):
    """View detected community clusters. Staff only."""
    db = get_db()
    rows = await query_communities(db)
    return Response.json(
        [
            {
                "community_id": r[0],
                "member_count": r[1],
                "members": r[2] if isinstance(r[2], list) else [],
            }
            for r in rows
        ]
    )


@app.post("/analytics/refresh-graph")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_refresh_graph(request):
    """Manually trigger graph analytics refresh. Staff only."""
    db = get_db()
    await run_graph_analytics(db)
    return Response.json({"ok": True, "message": "Graph analytics refreshed"})


@app.post("/analytics/reconcile-counts")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_reconcile_counts(request):
    """Reconcile denormalized forum counts with actual DB records. Staff only.

    Fixes subscriber_count and post_count drift caused by deletions, bans,
    or partial failures. Safe to run at any time.
    """
    db = get_db()
    result = await _reconcile_forum_counts_full(db)
    _cache.clear()
    return Response.json({"ok": True, **result})


@app.get("/analytics/count-drift")
@guard(*REQUIRE_LOGIN, Require.staff())
async def analytics_count_drift(request):
    """Show forums where denormalized counts differ from actual data. Staff only.

    Returns list of forums with drifted counts — useful for monitoring before
    triggering reconciliation.
    """
    db = get_db()
    rows = await db.query_tuples(
        """SELECT f.id, f.name, f.subscriber_count, f.post_count,
                  (SELECT COUNT(*) FROM hn_forum_members WHERE forum_id = f.id) AS actual_subs,
                  (SELECT COUNT(*) FROM hn_posts WHERE forum_id = f.id AND NOT is_deleted) AS actual_posts
           FROM hn_forums f
           WHERE f.subscriber_count != (SELECT COUNT(*) FROM hn_forum_members WHERE forum_id = f.id)
              OR f.post_count != (SELECT COUNT(*) FROM hn_posts WHERE forum_id = f.id AND NOT is_deleted)
           ORDER BY f.name"""
    )
    drifted = [
        {
            "forum": r[1],
            "subscriber_count": r[2],
            "actual_subscribers": r[4],
            "subscriber_drift": r[4] - r[2],
            "post_count": r[3],
            "actual_posts": r[5],
            "post_drift": r[5] - r[3],
        }
        for r in rows
    ]
    return Response.json({"drifted_forums": len(drifted), "forums": drifted})


# ---------------------------------------------------------------------------
# Routes: Bookmarks
# ---------------------------------------------------------------------------


@app.post("/bookmark")
@guard(*REQUIRE_ACTIVE)
async def toggle_bookmark(request):
    """Toggle a bookmark on a post or comment. Idempotent — add or remove."""
    try:
        data = await validate_form(request, BookmarkSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id
    uid = request.guard.active_user.id
    db = get_db()

    # Verify forum access
    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )

    if post_id:
        existing = await Bookmark.objects.filter(user_id=uid, post_id=post_id).first()
    elif comment_id:
        existing = await Bookmark.objects.filter(
            user_id=uid, comment_id=comment_id
        ).first()
    else:
        raise HTTPException(400, "post_id or comment_id required")

    if existing:
        await Bookmark.objects.filter(id=existing.id).delete()
        action = "removed"
    else:
        try:
            await Bookmark(user_id=uid, post_id=post_id, comment_id=comment_id).save()
        except IntegrityError:
            # Race: another request already created the bookmark, treat as toggle-off
            if post_id:
                await Bookmark.objects.filter(user_id=uid, post_id=post_id).delete()
            elif comment_id:
                await Bookmark.objects.filter(
                    user_id=uid, comment_id=comment_id
                ).delete()
            action = "removed"
        else:
            action = "added"

    if request.headers.get("hx-request"):
        label = "Unsave" if action == "added" else "Save"
        return Response.html(f'<span class="bookmark-status">{label}</span>')
    return Response.json({"ok": True, "action": action})


@app.get("/saved")
@guard(*REQUIRE_LOGIN)
async def saved_list(request):
    """Paginated list of bookmarked posts and comments."""
    uid = get_uid(request)
    filter_type = request.GET.get("type", "")  # "posts", "comments", or "" (all)
    cursor_param = request.GET.get("cursor", "")
    db = get_db()
    visible = await _visible_forum_ids(uid)
    visible_list = list(visible)

    keyset_where = ""
    params: list = [uid, visible_list]  # $1=uid, $2=visible array
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None
    if cursor_data and cursor_data[0] == "next":
        keyset_where = " AND b.id < $3"
        params.append(int(cursor_data[1]))
    limit_idx = len(params) + 1
    params.append(POSTS_PER_PAGE + 1)

    if filter_type == "comments":
        rows = await db.query_tuples(
            f"""SELECT b.id, b.comment_id, b.created_at,
                       c.text, c.score, c.post_id, p.title AS post_title
                FROM hn_bookmarks b
                JOIN hn_comments c ON c.id = b.comment_id
                JOIN hn_posts p ON p.id = c.post_id
                WHERE b.user_id = $1 AND b.comment_id > 0
                  AND p.forum_id = ANY($2::int[]){keyset_where}
                ORDER BY b.id DESC LIMIT ${limit_idx}""",
            *params,
        )
        items = [
            {
                "bookmark_id": r[0],
                "type": "comment",
                "comment_id": r[1],
                "text": r[3][:200] if r[3] else "",
                "score": r[4],
                "post_pid": Post._id_manager.encode(r[5]),
                "post_title": r[6],
                "time_ago": time_ago(r[2]),
            }
            for r in rows[:POSTS_PER_PAGE]
        ]
    elif filter_type == "posts":
        rows = await db.query_tuples(
            f"""SELECT b.id, b.post_id, b.created_at,
                       p.title, p.score, p.comment_count, p.url
                FROM hn_bookmarks b
                JOIN hn_posts p ON p.id = b.post_id
                WHERE b.user_id = $1 AND b.post_id > 0 AND NOT p.is_deleted
                  AND p.forum_id = ANY($2::int[]){keyset_where}
                ORDER BY b.id DESC LIMIT ${limit_idx}""",
            *params,
        )
        items = [
            {
                "bookmark_id": r[0],
                "type": "post",
                "post_id": r[1],
                "pid": Post._id_manager.encode(r[1]),
                "title": r[3],
                "score": r[4],
                "comment_count": r[5],
                "domain": domain_from_url(r[6]),
                "time_ago": time_ago(r[2]),
            }
            for r in rows[:POSTS_PER_PAGE]
        ]
    else:
        # All bookmarks — union via two queries (simpler than UNION SQL with different columns)
        post_rows = await db.query_tuples(
            f"""SELECT b.id, 'post' AS type, b.post_id, 0 AS comment_id, b.created_at,
                       p.title, p.score, p.comment_count, p.url, '' AS comment_text
                FROM hn_bookmarks b JOIN hn_posts p ON p.id = b.post_id
                WHERE b.user_id = $1 AND b.post_id > 0 AND NOT p.is_deleted
                  AND p.forum_id = ANY($2::int[]){keyset_where}
                ORDER BY b.id DESC LIMIT ${limit_idx}""",
            *params,
        )
        comment_rows = await db.query_tuples(
            f"""SELECT b.id, 'comment' AS type, c.post_id, b.comment_id, b.created_at,
                       p.title, c.score, 0, '', c.text
                FROM hn_bookmarks b
                JOIN hn_comments c ON c.id = b.comment_id
                JOIN hn_posts p ON p.id = c.post_id
                WHERE b.user_id = $1 AND b.comment_id > 0
                  AND p.forum_id = ANY($2::int[]){keyset_where}
                ORDER BY b.id DESC LIMIT ${limit_idx}""",
            *params,
        )
        # Merge and sort by bookmark id DESC
        all_rows = sorted(post_rows + comment_rows, key=lambda r: r[0], reverse=True)
        rows = all_rows[: POSTS_PER_PAGE + 1]
        items = []
        for r in rows[:POSTS_PER_PAGE]:
            item = {
                "bookmark_id": r[0],
                "type": r[1],
                "post_pid": Post._id_manager.encode(r[2]),
                "post_title": r[5],
                "score": r[6],
                "time_ago": time_ago(r[4]),
            }
            if r[1] == "post":
                item["comment_count"] = r[7]
                item["domain"] = domain_from_url(r[8])
            else:
                item["text"] = r[9][:200] if r[9] else ""
                item["comment_id"] = r[3]
            items.append(item)

    has_more = len(rows) > POSTS_PER_PAGE
    next_cursor = ""
    if has_more and rows:
        next_cursor = _encode_cursor("next", str(rows[POSTS_PER_PAGE - 1][0]))

    ctx = build_context(
        request, items=items, filter_type=filter_type, next_cursor=next_cursor
    )
    return app.render("saved.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Notifications
# ---------------------------------------------------------------------------


@app.get("/inbox")
@guard(*REQUIRE_LOGIN)
async def inbox(request):
    """Paginated notification inbox — unread first, then read."""
    uid = get_uid(request)
    cursor_param = request.GET.get("cursor", "")
    db = get_db()

    keyset_where = ""
    params: list = [uid]
    cursor_data = _decode_cursor(cursor_param) if cursor_param else None
    if cursor_data and cursor_data[0] == "next":
        keyset_where = " AND n.id < $2"
        params.append(int(cursor_data[1]))
    limit_idx = len(params) + 1
    params.append(POSTS_PER_PAGE + 1)

    rows = await db.query_tuples(
        f"""SELECT n.id, n.type, n.actor_id, n.post_id, n.comment_id,
                   n.message, n.is_read, n.created_at,
                   COALESCE(u.username, '') AS actor_name
            FROM hn_notifications n
            LEFT JOIN hn_users u ON u.id = n.actor_id AND n.actor_id > 0
            WHERE n.user_id = $1{keyset_where}
            ORDER BY n.id DESC LIMIT ${limit_idx}""",
        *params,
    )

    has_more = len(rows) > POSTS_PER_PAGE
    display_rows = rows[:POSTS_PER_PAGE]

    notifications = []
    for r in display_rows:
        notif = {
            "id": r[0],
            "type": r[1],
            "actor": r[8],
            "message": r[5],
            "is_read": r[6],
            "time_ago": time_ago(r[7]),
        }
        if r[3]:
            notif["post_pid"] = Post._id_manager.encode(r[3])
        if r[4]:
            notif["comment_id"] = r[4]
        notifications.append(notif)

    next_cursor = ""
    if has_more and display_rows:
        next_cursor = _encode_cursor("next", str(display_rows[-1][0]))

    ctx = build_context(request, notifications=notifications, next_cursor=next_cursor)
    return app.render("inbox.html", ctx)


@app.post("/inbox/read")
@guard(*REQUIRE_LOGIN)
async def inbox_mark_read(request):
    """Mark all notifications as read."""
    uid = get_uid(request)
    await Notification.objects.filter(user_id=uid, is_read=False).update(is_read=True)
    return Response.redirect("/inbox")


@app.get("/inbox/count")
@guard(*REQUIRE_LOGIN)
async def inbox_count(request):
    """Return unread notification count. HTML for HTMX badge, JSON for API."""
    uid = get_uid(request)
    n = await Notification.objects.filter(user_id=uid, is_read=False).count()
    if request.headers.get("hx-request"):
        badge = f"({n})" if n else ""
        return Response.html(badge)
    return Response.json({"unread": n})


# ---------------------------------------------------------------------------
# Routes: Extended user profile
# ---------------------------------------------------------------------------


@app.get("/user/{username}/karma")
async def user_karma(request, username: str):
    """Karma breakdown by forum."""
    profile_user = await User.objects.filter(username=username).first()
    if not profile_user:
        raise HTTPException(404, "User not found")
    visible = await _visible_forum_ids(get_uid_or_none(request))
    visible_list = list(visible)
    db = get_db()
    rows = await db.query_tuples(
        """SELECT COALESCE(f.name, 'global') AS forum_name,
                   SUM(p.score) AS total_score,
                   COUNT(p.id) AS post_count
            FROM hn_posts p LEFT JOIN hn_forums f ON f.id = p.forum_id
            WHERE p.author_id = $1 AND NOT p.is_deleted
              AND p.forum_id = ANY($2::int[])
            GROUP BY f.name
            ORDER BY total_score DESC""",
        profile_user.id,
        visible_list,
    )
    breakdown = [
        {"forum": r[0] or "global", "score": r[1], "posts": r[2]} for r in rows
    ]
    return Response.json(
        {
            "username": profile_user.username,
            "total_karma": profile_user.karma,
            "by_forum": breakdown,
        }
    )


@app.get("/settings/profile")
@guard(*REQUIRE_LOGIN)
async def profile_settings_form(request):
    """Extended profile settings form."""
    uid = get_uid(request)
    user = await get_current_user(request)
    if not user:
        return Response.redirect("/login")
    profile = await UserProfile.objects.filter(user_id=uid).first()
    ctx = build_context(
        request,
        account={
            "display_name": user.display_name,
            "bio": user.bio,
            "email": user.email,
            "website": profile.website if profile else "",
            "location": profile.location if profile else "",
            "avatar_url": profile.avatar_url if profile else "",
            "github_username": profile.github_username if profile else "",
        },
    )
    return app.render("profile_settings.html", ctx)


@app.post("/settings/profile")
@guard(*REQUIRE_ACTIVE)
async def profile_settings_handler(request):
    """Handle extended profile settings update."""
    uid = get_uid(request)
    user = request.guard.active_user

    try:
        data = await validate_form(request, ProfileSettingsSchema)
    except ValidationErrors as exc:
        ctx = build_context(
            request,
            account={
                "display_name": user.display_name,
                "bio": user.bio,
                "email": user.email,
            },
            errors=[str(e) for e in exc.errors],
        )
        return app.render("profile_settings.html", ctx, status=400)

    display_name = data.display_name
    bio = data.bio
    email = data.email
    website = data.website
    location = data.location
    avatar_url = data.avatar_url
    github_username = data.github_username

    await User.objects.filter(id=uid).update(
        display_name=display_name,
        bio=bio,
        email=email,
    )

    # Upsert profile
    existing = await UserProfile.objects.filter(user_id=uid).first()
    if existing:
        await UserProfile.objects.filter(id=existing.id).update(
            website=website,
            location=location,
            avatar_url=avatar_url,
            github_username=github_username,
        )
    else:
        await UserProfile(
            user_id=uid,
            website=website,
            location=location,
            avatar_url=avatar_url,
            github_username=github_username,
        ).save()

    ctx = build_context(
        request,
        account={
            "display_name": display_name,
            "bio": bio,
            "email": email,
            "website": website,
            "location": location,
            "avatar_url": avatar_url,
            "github_username": github_username,
        },
        success="Profile updated",
    )
    return app.render("profile_settings.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Drafts & Post Editing (P2)
# ---------------------------------------------------------------------------


@app.post("/post/{pid}/edit")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("post_access", resolver=_resolve_post_edit, from_path="pid"),
)
async def edit_post(request, pid: str):
    """Edit a post. Creates a revision for history tracking."""
    access = request.guard.post_access
    post = access.post

    try:
        data = await validate_form(request, EditPostSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    title = data.title
    text = data.text
    edit_reason = data.edit_reason

    # Save revision before updating — transaction ensures atomicity
    db = get_db()
    async with db.transaction():
        await PostRevision(
            post_id=post.id,
            title=post.title,
            text=post.text,
            edited_by=request.guard.active_user.id,
            edit_reason=edit_reason,
        ).save()
        await Post.objects.filter(id=post.id).update(title=title, text=text)
    _cache.clear()
    return Response.redirect(f"/post/{pid}")


async def _fetch_post_history_data(post_id: int) -> list[dict[str, str]]:
    """Fetch post revision history with editor names. Shared by JSON and HTML routes."""
    revisions = (
        await PostRevision.objects.filter(post_id=post_id).order_by("-created_at").all()
    )

    # Batch fetch editor usernames
    editor_ids = list({r.edited_by for r in revisions})
    editors: dict[int, str] = {}
    if editor_ids:
        editor_rows = await User.objects.filter(id__in=editor_ids).all()
        editors = {u.id: u.username for u in editor_rows}

    return [
        {
            "title": r.title,
            "text": r.text[:500],
            "editor": editors.get(r.edited_by, "unknown"),
            "reason": r.edit_reason,
            "time_ago": time_ago(r.created_at),
        }
        for r in revisions
    ]


@app.get("/post/{pid}/history")
async def post_history(request, pid: str):
    """View edit history for a post."""
    access = await resolve_post(request, pid)
    rev_list = await _fetch_post_history_data(access.post.id)
    return Response.json(
        {
            "revision_count": len(rev_list),
            "revisions": rev_list,
        }
    )


@app.get("/post/{pid}/history/view")
async def post_history_view(request, pid: str):
    """Render HTML edit history page for a post."""
    access = await resolve_post(request, pid)
    rev_list = await _fetch_post_history_data(access.post.id)
    ctx = build_context(
        request, post={"pid": pid, "title": access.post.title}, revisions=rev_list
    )
    return app.render("post_history.html", ctx)


@app.post("/draft")
@guard(*REQUIRE_ACTIVE)
async def save_draft(request):
    """Save a post as draft (not publicly visible)."""
    user = request.guard.active_user
    try:
        data = await validate_form(request, DraftSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    title = data.title
    text = data.text
    forum_name = data.forum

    forum_id = 0
    if forum_name:
        # SECURITY FIX: enforce forum access (private, archived, locked) for drafts too
        forum_access = await resolve_forum(request, forum_name, ForumIntent.WRITE_POST)
        forum_id = forum_access.forum.id

    slug = slugify(title) or "draft"
    post = Post(
        title=title,
        slug=slug,
        text=text,
        author_id=user.id,
        forum_id=forum_id,
        status=PostStatus.DRAFT,
    )
    await post.save()
    return Response.redirect("/drafts")


@app.get("/drafts")
@guard(*REQUIRE_LOGIN)
async def drafts_list(request):
    """List the current user's draft posts."""
    uid = get_uid(request)
    drafts = (
        await Post.objects.filter(
            author_id=uid, status=PostStatus.DRAFT.value, is_deleted=False
        )
        .order_by("-created_at")
        .limit(50)
        .all()
    )
    draft_list = [
        {
            "pid": d.get_external_id(),
            "title": d.title,
            "text": d.text[:200] if d.text else "",
            "time_ago": time_ago(d.created_at),
        }
        for d in drafts
    ]
    ctx = build_context(request, drafts=draft_list)
    return app.render("drafts.html", ctx)


@app.post("/post/{pid}/publish")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("post_access", resolver=_resolve_post_edit, from_path="pid"),
)
async def publish_draft(request, pid: str):
    """Publish a draft post."""
    access = request.guard.post_access
    post = access.post
    post_status = _normalize_status(post.status)
    if post_status != "draft":
        raise HTTPException(400, "Post is already published")

    db = get_db()
    async with db.transaction():
        await Post.objects.filter(id=post.id).update(status=PostStatus.PUBLISHED.value)
        if post.forum_id:
            await Forum.objects.filter(id=post.forum_id).update(
                post_count=F("post_count") + 1
            )
    _cache.clear()
    return Response.redirect(f"/post/{pid}")


# ---------------------------------------------------------------------------
# Routes: Polls (P2)
# ---------------------------------------------------------------------------


@app.get("/post/{pid}/poll/create")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("post_access", resolver=_resolve_post_author, from_path="pid"),
)
async def poll_create_page(request, pid: str):
    """Form to create a poll on an existing post (author only)."""
    access = request.guard.post_access
    post = access.post
    ctx = build_context(request, post={"pid": pid, "title": post.title})
    return app.render("poll_create.html", ctx)


@app.post("/post/{pid}/poll")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource(
        "post_access", resolver=_resolve_post_author_not_archived, from_path="pid"
    ),
)
async def create_poll(request, pid: str):
    """Attach a poll to an existing post (post author only)."""
    access = request.guard.post_access
    post = access.post

    # Check no poll exists yet. Presence-only → `.exists()` is cheaper.
    if await Poll.objects.filter(post_id=post.id).exists():
        raise HTTPException(400, "This post already has a poll")

    try:
        data = await validate_form(request, PollCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    try:
        poll_type = PollType(data.poll_type)
    except ValueError:
        poll_type = PollType.SINGLE_CHOICE
    options_raw = data.options

    options = [o.strip() for o in options_raw.split("\n") if o.strip()]
    if len(options) < 2:
        raise HTTPException(400, "At least 2 options required")

    db = get_db()
    async with db.transaction():
        poll = Poll(post_id=post.id, question=data.question, poll_type=poll_type)
        await poll.save()
        for i, opt_text in enumerate(options):
            await PollOption(poll_id=poll.id, text=opt_text, position=i).save()

    return Response.redirect(f"/post/{pid}")


@app.post("/poll/{poll_id}/vote")
@guard(*REQUIRE_ACTIVE)
async def poll_vote(request, poll_id: int):
    """Vote on a poll option."""
    poll = await Poll.objects.filter(id=poll_id).first()
    if not poll:
        raise HTTPException(404, "Poll not found")

    # Enforce forum access via post
    db = get_db()
    await require_target_forum_access(request, db, post_id=poll.post_id)

    uid = request.guard.active_user.id
    try:
        data = await validate_form(request, PollVoteSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    option_id = data.option_id

    option = await PollOption.objects.filter(id=option_id, poll_id=poll.id).first()
    if not option:
        raise HTTPException(400, "Invalid option")

    # Check if already voted on this option
    existing = await PollVote.objects.filter(
        poll_id=poll.id, user_id=uid, option_id=option_id
    ).first()
    if existing:
        # Toggle off
        async with db.transaction():
            await PollVote.objects.filter(id=existing.id).delete()
            await db.execute(
                "UPDATE hn_poll_options SET vote_count = GREATEST(vote_count - 1, 0) WHERE id = $1",
                option_id,
            )
        return Response.json({"ok": True, "action": "removed"})

    # Single transaction: remove old votes (single-choice) + add new vote
    poll_type_val = (
        poll.poll_type.value if isinstance(poll.poll_type, PollType) else poll.poll_type
    )
    async with db.transaction():
        if poll_type_val == PollType.SINGLE_CHOICE.value:
            old_votes = await PollVote.objects.filter(
                poll_id=poll.id, user_id=uid
            ).all()
            if old_votes:
                old_option_ids = {ov.option_id for ov in old_votes}
                await PollVote.objects.filter(poll_id=poll.id, user_id=uid).delete()
                for oid in old_option_ids:
                    await db.execute(
                        "UPDATE hn_poll_options SET vote_count = GREATEST(vote_count - 1, 0) WHERE id = $1",
                        oid,
                    )
        await PollVote(poll_id=poll.id, option_id=option_id, user_id=uid).save()
        await db.execute(
            "UPDATE hn_poll_options SET vote_count = vote_count + 1 WHERE id = $1",
            option_id,
        )

    return Response.json({"ok": True, "action": "voted"})


@app.get("/poll/{poll_id}/results")
async def poll_results(request, poll_id: int):
    """Get poll results as JSON."""
    poll = await Poll.objects.filter(id=poll_id).first()
    if not poll:
        raise HTTPException(404, "Poll not found")

    db = get_db()
    await require_target_forum_access(request, db, post_id=poll.post_id)

    options = (
        await PollOption.objects.filter(poll_id=poll.id).order_by("position").all()
    )
    total = sum(o.vote_count for o in options)

    return Response.json(
        {
            "question": poll.question,
            "poll_type": poll.poll_type
            if isinstance(poll.poll_type, str)
            else poll.poll_type.value,
            "total_votes": total,
            "options": [
                {
                    "id": o.id,
                    "text": o.text,
                    "votes": o.vote_count,
                    "percent": round(o.vote_count / total * 100, 1) if total else 0,
                }
                for o in options
            ],
        }
    )


# ---------------------------------------------------------------------------
# Routes: Crossposting (P2)
# ---------------------------------------------------------------------------


@app.get("/post/{pid}/crosspost")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("post_access", resolver=_resolve_post_view, from_path="pid"),
)
async def crosspost_page(request, pid: str):
    """Form to crosspost a post to another forum."""
    access = request.guard.post_access
    post = access.post
    uid = get_uid(request)

    # Get list of forums the user can post to
    memberships = await ForumMember.objects.filter(user_id=uid).all()
    forum_ids = [m.forum_id for m in memberships]
    target_forums = []
    if forum_ids:
        forums = await Forum.objects.filter(id__in=forum_ids).all()
        target_forums = []
        for f in forums:
            if f.id == (post.forum_id or 0):
                continue
            if await f.has_status("archive", "archived"):
                continue
            if await f.has_status("lock", "locked"):
                continue
            target_forums.append({"name": f.name, "title": f.title})

    ctx = build_context(
        request, post={"pid": pid, "title": post.title}, target_forums=target_forums
    )
    return app.render("crosspost.html", ctx)


@app.post("/post/{pid}/crosspost")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("post_access", resolver=_resolve_post_published, from_path="pid"),
)
async def crosspost(request, pid: str):
    """Crosspost a post to another forum."""
    access = request.guard.post_access
    source = access.post

    try:
        data = await validate_form(request, CrosspostSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    target_access = await resolve_forum(request, data.forum, ForumIntent.WRITE_POST)
    target_forum = target_access.forum

    # Prevent crossposting to the same forum
    if target_forum.id == source.forum_id:
        raise HTTPException(400, "Cannot crosspost to the same forum")

    # Prevent duplicate crosspost to same forum
    existing_xpost = await Post.objects.filter(
        crosspost_source_id=source.id, forum_id=target_forum.id, is_deleted=False
    ).first()
    if existing_xpost:
        raise HTTPException(400, "This post has already been crossposted to that forum")

    uid = request.guard.active_user.id
    slug = slugify(source.title) or "crosspost"
    db = get_db()

    async with db.transaction():
        xpost = Post(
            title=source.title,
            slug=slug,
            url=source.url,
            text=source.text,
            author_id=uid,
            forum_id=target_forum.id,
            crosspost_source_id=source.id,
        )
        await xpost.save()
        await Forum.objects.filter(id=target_forum.id).update(
            post_count=F("post_count") + 1
        )
    _cache.clear()
    return Response.redirect(f"/post/{xpost.get_external_id()}")


# ---------------------------------------------------------------------------
# Routes: User Flairs (P2)
# ---------------------------------------------------------------------------


_FLAIR_CSS_CLASSES = frozenset({"custom", "verified", "contributor", "patron"})
_FLAIR_MOD_CSS_CLASSES = frozenset({"mod", "admin"}) | _FLAIR_CSS_CLASSES


@app.get("/f/{forum_name}/flair")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_read, from_path="forum_name"),
)
async def flair_manage_page(request, forum_name: str):
    """Flair management page — view/set flair. Mods see all flairs."""
    access = request.guard.access
    forum = access.forum
    uid = get_uid(request)

    # Get current user's flair for this forum
    my_flair = await UserFlair.objects.filter(user_id=uid, forum_id=forum.id).first()
    my_flair_dict = None
    if my_flair:
        my_flair_dict = {
            "text": my_flair.text,
            "css_class": my_flair.css_class,
            "self_assigned": my_flair.assigned_by == 0,
        }

    # If mod, get all flairs for this forum
    all_flairs = []
    if access.is_mod:
        flairs = await UserFlair.objects.filter(forum_id=forum.id).all()
        flair_user_ids = list({f.user_id for f in flairs})
        flair_users: dict[int, str] = {}
        if flair_user_ids:
            flair_user_rows = await User.objects.filter(id__in=flair_user_ids).all()
            flair_users = {u.id: u.username for u in flair_user_rows}
        all_flairs = [
            {
                "username": flair_users.get(f.user_id, "unknown"),
                "text": f.text,
                "css_class": f.css_class,
                "self_assigned": f.assigned_by == 0,
            }
            for f in flairs
        ]

    ctx = build_context(
        request,
        forum=await _forum_to_dict(forum),
        my_flair=my_flair_dict,
        all_flairs=all_flairs,
        is_mod=access.is_mod,
    )
    return app.render("flair_manage.html", ctx)


@app.post("/f/{forum_name}/flair")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_read, from_path="forum_name"),
)
async def set_flair(request, forum_name: str):
    """Set or update a user's flair in a forum. Mods can set for others."""
    access = request.guard.access
    forum = access.forum
    if not access.is_member:
        raise HTTPException(403, "You must be a forum member to set flair")

    try:
        data = await validate_form(request, FlairSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    target_username = data.username
    flair_text = data.flair_text
    css_class = data.css_class

    uid = request.guard.active_user.id
    caller_is_mod = access.is_mod

    # CSS class whitelist — mods can use "mod"/"admin", regular users cannot
    allowed = _FLAIR_MOD_CSS_CLASSES if caller_is_mod else _FLAIR_CSS_CLASSES
    if css_class not in allowed:
        css_class = "custom"

    # Determine target user
    if target_username and target_username != request.guard.active_user.username:
        # Setting flair for another user — must be mod
        if not caller_is_mod:
            raise HTTPException(403, "Only moderators can set flairs for other users")
        target = await User.objects.filter(username=target_username).first()
        if not target:
            raise HTTPException(404, "User not found")
        target_uid = target.id
        assigned_by = uid
    else:
        target_uid = uid
        assigned_by = 0  # Self-assigned

    existing = await UserFlair.objects.filter(
        user_id=target_uid, forum_id=forum.id
    ).first()
    if existing:
        await UserFlair.objects.filter(id=existing.id).update(
            text=flair_text,
            css_class=css_class,
            assigned_by=assigned_by,
        )
    else:
        await UserFlair(
            user_id=target_uid,
            forum_id=forum.id,
            text=flair_text,
            css_class=css_class,
            assigned_by=assigned_by,
        ).save()

    return Response.json({"ok": True, "flair": flair_text})


@app.get("/f/{forum_name}/flair/{username}")
async def get_flair(request, forum_name: str, username: str):
    """Get a user's flair for a specific forum."""
    access = await resolve_forum(request, forum_name, ForumIntent.READ)
    forum = access.forum
    user = await User.objects.filter(username=username).first()
    if not user:
        raise HTTPException(404, "User not found")

    flair = await UserFlair.objects.filter(user_id=user.id, forum_id=forum.id).first()
    if not flair:
        return Response.json({"flair": None})
    return Response.json(
        {
            "flair": flair.text,
            "css_class": flair.css_class,
            "self_assigned": flair.assigned_by == 0,
        }
    )


# ---------------------------------------------------------------------------
# Routes: RSS Feeds (P2)
# ---------------------------------------------------------------------------


@app.get("/feed/rss")
async def rss_feed_global(request):
    """Global RSS feed — hot posts from all public forums."""
    visible = await _visible_forum_ids(None)  # Public only for RSS
    visible_list = list(visible)
    db = get_db()
    rows = await db.query_tuples(
        """SELECT p.id, p.title, p.url, p.text, p.score, p.created_at,
                   u.username, p.slug
            FROM hn_posts p JOIN hn_users u ON u.id = p.author_id
            WHERE NOT p.is_deleted AND p.status = 'published'
              AND p.forum_id = ANY($1::int[])
            ORDER BY p.hot_score DESC, p.id DESC LIMIT 30""",
        visible_list,
    )
    return _build_rss("HyperNews", "Latest from HyperNews", "/", rows)


@app.get("/f/{forum_name}/feed/rss")
async def rss_feed_forum(request, forum_name: str):
    """Per-forum RSS feed."""
    access = await resolve_forum(request, forum_name, ForumIntent.READ)
    forum = access.forum
    if not forum.is_public:
        raise HTTPException(404, "Forum not found")
    db = get_db()
    rows = await db.query_tuples(
        """SELECT p.id, p.title, p.url, p.text, p.score, p.created_at,
                  u.username, p.slug
           FROM hn_posts p JOIN hn_users u ON u.id = p.author_id
           WHERE p.forum_id = $1 AND NOT p.is_deleted AND p.status = 'published'
           ORDER BY p.created_at DESC LIMIT 30""",
        forum.id,
    )
    return _build_rss(f"f/{forum.name}", forum.title, f"/f/{forum.name}/", rows)


@app.get("/user/{username}/feed/rss")
async def rss_feed_user(request, username: str):
    """Per-user RSS feed (public posts only)."""
    user = await User.objects.filter(username=username).first()
    if not user:
        raise HTTPException(404, "User not found")
    visible = await _visible_forum_ids(None)
    visible_list = list(visible)
    db = get_db()
    rows = await db.query_tuples(
        """SELECT p.id, p.title, p.url, p.text, p.score, p.created_at,
                   u.username, p.slug
            FROM hn_posts p JOIN hn_users u ON u.id = p.author_id
            WHERE p.author_id = $1 AND NOT p.is_deleted AND p.status = 'published'
              AND p.forum_id = ANY($2::int[])
            ORDER BY p.created_at DESC LIMIT 30""",
        user.id,
        visible_list,
    )
    return _build_rss(
        f"{username}'s posts", f"Posts by {username}", f"/user/{username}", rows
    )


def _build_rss(title: str, description: str, link: str, rows: list) -> Response:
    """Build RSS 2.0 XML response from post rows.

    Columns: 0=id, 1=title, 2=url, 3=text, 4=score, 5=created_at, 6=username, 7=slug
    """
    items = []
    for r in rows:
        post_obj = Post(id=r[0])
        post_url = r[2] or f"/post/{post_obj.get_external_id()}"
        item_desc = _html.escape(r[3][:500]) if r[3] else f"{r[4]} points"
        items.append(
            f"<item>"
            f"<title>{_html.escape(r[1])}</title>"
            f"<link>{_html.escape(post_url)}</link>"
            f"<description>{item_desc}</description>"
            f"<author>{_html.escape(r[6])}</author>"
            f"<pubDate>{r[5]}</pubDate>"
            f"</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0">'
        f"<channel>"
        f"<title>{_html.escape(title)}</title>"
        f"<link>{_html.escape(link)}</link>"
        f"<description>{_html.escape(description)}</description>"
        + "".join(items)
        + "</channel></rss>"
    )
    return Response(body=xml.encode(), content_type="application/rss+xml")


# ---------------------------------------------------------------------------
# Routes: Content Pinning (P3)
# ---------------------------------------------------------------------------

MAX_PINNED_PER_FORUM = 3


@app.post("/f/{forum_name}/pin/{pid}")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource(
        "forum_access", resolver=_resolve_forum_moderate, from_path="forum_name"
    ),
    Require.resource("post_access", resolver=_resolve_post_view, from_path="pid"),
)
async def pin_post(request, forum_name: str, pid: str):
    """Pin a post to the top of a forum (mod/admin only). Max 3 pinned."""
    forum_access = request.guard.forum_access
    forum = forum_access.forum
    uid = get_uid(request)
    check_action_rate_limit(uid, "pin", 10, 60)

    post_access = request.guard.post_access
    post = post_access.post
    if post.forum_id != forum.id:
        raise HTTPException(400, "Post does not belong to this forum")

    # Check pin limit
    pinned_count = await Post.objects.filter(
        forum_id=forum.id, is_pinned=True, is_deleted=False
    ).count()
    if pinned_count >= MAX_PINNED_PER_FORUM and not post.is_pinned:
        raise HTTPException(
            400, f"Maximum {MAX_PINNED_PER_FORUM} pinned posts per forum"
        )

    new_state = not post.is_pinned  # Toggle
    await Post.objects.filter(id=post.id).update(
        is_pinned=new_state, pinned_by=uid if new_state else 0
    )
    await _log_forum_action(
        uid, forum.id, "pin" if new_state else "unpin", reason=f"post {pid}"
    )
    _cache.clear()
    return Response.json({"ok": True, "is_pinned": new_state})


# ---------------------------------------------------------------------------
# Routes: Post Awards (P3)
# ---------------------------------------------------------------------------

AWARD_KARMA_COST = 10


@app.get("/post/{pid}/award")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("post_access", resolver=_resolve_post_view, from_path="pid"),
)
async def award_page(request, pid: str):
    """Form to give an award to a post."""
    access = request.guard.post_access
    post = access.post
    award_types = [
        {"value": at.value, "label": at.value.replace("_", " ").title()}
        for at in AwardType
    ]
    ctx = build_context(
        request,
        post={"pid": pid, "title": post.title, "id": post.id},
        award_types=award_types,
    )
    return app.render("award_give.html", ctx)


@app.post("/award")
@guard(*REQUIRE_ACTIVE)
async def give_award(request):
    """Give an award to a post or comment. Costs karma."""
    uid = request.guard.active_user.id
    check_action_rate_limit(uid, "award", 5, 60)

    try:
        data = await validate_form(request, AwardSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    post_id = data.post_id
    comment_id = data.comment_id

    if not post_id and not comment_id:
        raise HTTPException(400, "post_id or comment_id required")

    try:
        award_type = AwardType(data.award_type)
    except ValueError:
        award_type = AwardType.INSIGHTFUL

    user = request.guard.active_user
    if user.karma < AWARD_KARMA_COST:
        raise HTTPException(
            400,
            f"Need at least {AWARD_KARMA_COST} karma to give awards (you have {user.karma})",
        )

    db = get_db()
    await require_target_forum_access(
        request, db, post_id=post_id, comment_id=comment_id
    )

    # Self-award prevention
    if post_id:
        target = await Post.objects.filter(id=post_id).first()
        if target and target.author_id == user.id:
            raise HTTPException(400, "Cannot award your own content")
    elif comment_id:
        target = await Comment.objects.filter(id=comment_id).first()
        if target and target.author_id == user.id:
            raise HTTPException(400, "Cannot award your own content")

    # Duplicate award prevention
    existing_award = await Award.objects.filter(
        user_id=user.id,
        post_id=post_id or 0,
        comment_id=comment_id or 0,
        award_type=award_type.value,
    ).first()
    if existing_award:
        raise HTTPException(400, "You already gave this award to this content")

    # Deduct karma and create award atomically
    async with db.transaction():
        await User.objects.filter(id=user.id).update(
            karma=F("karma") - AWARD_KARMA_COST
        )
        await Award(
            post_id=post_id,
            comment_id=comment_id,
            user_id=user.id,
            award_type=award_type,
        ).save()
        # Notify the content author
        if post_id:
            post = await Post.objects.filter(id=post_id).first()
            if post:
                await _notify(
                    post.author_id,
                    NotificationType.SYSTEM,
                    f"{user.username} gave your post a '{award_type.value}' award",
                    actor_id=user.id,
                    post_id=post_id,
                )
        elif comment_id:
            cmt = await Comment.objects.filter(id=comment_id).first()
            if cmt:
                await _notify(
                    cmt.author_id,
                    NotificationType.SYSTEM,
                    f"{user.username} gave your comment a '{award_type.value}' award",
                    actor_id=user.id,
                    comment_id=comment_id,
                )

    return Response.json({"ok": True, "award_type": award_type.value})


@app.get("/awards/{content_id}")
async def get_awards(request, content_id: int):
    """Get grouped award counts for a post or comment ID."""
    db = get_db()
    # Enforce private forum access — prevent info disclosure from private forums
    await require_target_forum_access(request, db, post_id=content_id)
    rows = await db.query_tuples(
        """SELECT award_type, COUNT(*) FROM hn_awards
           WHERE post_id = $1 OR comment_id = $1
           GROUP BY award_type""",
        content_id,
    )
    return Response.json({"awards": [{"type": r[0], "count": r[1]} for r in rows]})


# ---------------------------------------------------------------------------
# Routes: Sequences / Series (P3)
# ---------------------------------------------------------------------------


async def _fetch_sequences_list_data() -> list[dict[str, str | int]]:
    """Fetch public sequences with author names. Shared by JSON and HTML routes."""
    sequences = (
        await Sequence.objects.filter(is_public=True)
        .order_by("-created_at")
        .limit(50)
        .all()
    )
    # Batch fetch authors
    author_ids = list({s.author_id for s in sequences})
    authors: dict[int, str] = {}
    if author_ids:
        author_rows = await User.objects.filter(id__in=author_ids).all()
        authors = {u.id: u.username for u in author_rows}

    return [
        {
            "id": s.id,
            "title": s.title,
            "description": s.description[:200] if s.description else "",
            "author": authors.get(s.author_id, "unknown"),
            "time_ago": time_ago(s.created_at),
        }
        for s in sequences
    ]


@app.get("/sequences")
async def sequences_list(request):
    """Browse all public sequences."""
    seq_list = await _fetch_sequences_list_data()
    return Response.json({"sequences": seq_list})


@app.get("/sequences/browse")
async def sequences_list_view(request):
    """Render HTML page to browse all public sequences."""
    seq_list = await _fetch_sequences_list_data()
    ctx = build_context(request, sequences=seq_list)
    return app.render("sequences.html", ctx)


@app.get("/sequences/create")
@guard(*REQUIRE_LOGIN)
async def sequence_create_page(request):
    """Form to create a new sequence."""
    ctx = build_context(request)
    return app.render("sequence_create.html", ctx)


@app.post("/sequences")
@guard(*REQUIRE_ACTIVE)
async def create_sequence(request):
    """Create a new sequence."""
    check_action_rate_limit(request.guard.active_user.id, "sequence", 3, 60)

    try:
        data = await validate_form(request, SequenceCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    seq = Sequence(
        title=data.title,
        description=data.description,
        author_id=request.guard.active_user.id,
    )
    await seq.save()
    return Response.json({"ok": True, "id": seq.id})


async def _fetch_sequence_data(
    request, seq_id: int
) -> tuple[Sequence, str, list[dict[str, str | int]], bool]:
    """Fetch sequence, author name, entries, and is_author flag. Shared by JSON and HTML routes.

    Returns (sequence, author_name, post_list, is_author).
    Raises HTTPException if not found or not visible.
    """
    seq = await Sequence.objects.filter(id=seq_id).first()
    if not seq:
        raise HTTPException(404, "Sequence not found")
    uid = get_uid_or_none(request)
    if not seq.is_public:
        if uid != seq.author_id:
            raise HTTPException(404, "Sequence not found")

    db = get_db()
    visible = await _visible_forum_ids(uid)
    visible_list = list(visible)

    entries = await db.query_tuples(
        """SELECT se.position, p.id, p.title, p.score, p.comment_count
            FROM hn_sequence_entries se JOIN hn_posts p ON p.id = se.post_id
            WHERE se.sequence_id = $1 AND NOT p.is_deleted
              AND p.forum_id = ANY($2::int[])
            ORDER BY se.position""",
        seq.id,
        visible_list,
    )

    author = await User.objects.filter(id=seq.author_id).first()
    author_name = author.username if author else "unknown"

    post_list = []
    for r in entries:
        post_obj = Post(id=r[1])
        post_list.append(
            {
                "position": r[0],
                "pid": post_obj.get_external_id(),
                "title": r[2],
                "score": r[3],
                "comment_count": r[4],
            }
        )

    return seq, author_name, post_list, uid == seq.author_id


@app.get("/sequence/{seq_id}")
async def sequence_detail(request, seq_id: int):
    """View a sequence — table of contents with posts."""
    seq, author_name, post_list, is_author = await _fetch_sequence_data(request, seq_id)
    return Response.json(
        {
            "id": seq.id,
            "title": seq.title,
            "description": seq.description,
            "author": author_name,
            "entry_count": len(post_list),
            "entries": post_list,
            "is_author": is_author,
        }
    )


@app.get("/sequence/{seq_id}/view")
async def sequence_detail_view(request, seq_id: int):
    """Render HTML page for a sequence — table of contents with posts."""
    seq, author_name, post_list, is_author = await _fetch_sequence_data(request, seq_id)
    ctx = build_context(
        request,
        sequence={
            "id": seq.id,
            "title": seq.title,
            "description": seq.description,
            "author": author_name,
            "entry_count": len(post_list),
        },
        entries=post_list,
        is_author=is_author,
    )
    return app.render("sequence_detail.html", ctx)


@app.post("/sequence/{seq_id}/add")
@guard(*REQUIRE_ACTIVE)
async def sequence_add_post(request, seq_id: int):
    """Add a post to a sequence (author only)."""
    seq = await Sequence.objects.filter(id=seq_id).first()
    if not seq:
        raise HTTPException(404, "Sequence not found")
    if seq.author_id != request.guard.active_user.id:
        raise HTTPException(403, "Only the sequence author can add posts")

    try:
        data = await validate_form(request, SequenceAddSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    pid = data.pid
    access = await resolve_post(request, pid, require_published=True)

    # Check not already in sequence
    existing = await SequenceEntry.objects.filter(
        sequence_id=seq.id, post_id=access.post.id
    ).first()
    if existing:
        raise HTTPException(400, "Post already in this sequence")

    # Get next position
    last = (
        await SequenceEntry.objects.filter(sequence_id=seq.id)
        .order_by("-position")
        .first()
    )
    next_pos = (last.position + 1) if last else 0

    await SequenceEntry(
        sequence_id=seq.id,
        post_id=access.post.id,
        position=next_pos,
    ).save()
    return Response.json({"ok": True, "position": next_pos})


@app.post("/sequence/{seq_id}/remove")
@guard(*REQUIRE_ACTIVE)
async def sequence_remove_post(request, seq_id: int):
    """Remove a post from a sequence (author only)."""
    seq = await Sequence.objects.filter(id=seq_id).first()
    if not seq:
        raise HTTPException(404, "Sequence not found")
    if seq.author_id != request.guard.active_user.id:
        raise HTTPException(403, "Only the sequence author can remove posts")

    try:
        data = await validate_form(request, SequenceAddSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    pid = data.pid
    access = await resolve_post(request, pid)

    await SequenceEntry.objects.filter(
        sequence_id=seq.id, post_id=access.post.id
    ).delete()
    return Response.json({"ok": True})


# ---------------------------------------------------------------------------
# Routes: Related Posts (P3)
# ---------------------------------------------------------------------------


@app.get("/post/{pid}/related")
async def related_posts(request, pid: str):
    """Get related posts — same forum, similar score range, recent."""
    access = await resolve_post(request, pid)
    post = access.post
    db = get_db()

    visible = await _visible_forum_ids(get_uid_or_none(request))
    visible_list = list(visible)

    # Strategy: posts in same forum with similar score range (index-friendly, no ABS scan)
    score_range = max(post.score // 2, 20)  # Dynamic range based on post score
    if post.forum_id:
        rows = await db.query_tuples(
            """SELECT p.id, p.title, p.score, p.comment_count
                FROM hn_posts p
                WHERE p.forum_id = $1 AND p.id != $2
                  AND NOT p.is_deleted AND (p.status = 'published' OR p.status IS NULL)
                  AND NOT COALESCE(p.is_pinned, false)
                  AND p.score BETWEEN $3 AND $4
                ORDER BY p.created_at DESC
                LIMIT 5""",
            post.forum_id,
            post.id,
            post.score - score_range,
            post.score + score_range,
        )
    else:
        rows = await db.query_tuples(
            """SELECT p.id, p.title, p.score, p.comment_count
                FROM hn_posts p
                WHERE p.id != $1
                  AND NOT p.is_deleted AND (p.status = 'published' OR p.status IS NULL)
                  AND NOT COALESCE(p.is_pinned, false)
                  AND p.forum_id = ANY($2::int[])
                  AND p.score BETWEEN $3 AND $4
                ORDER BY p.created_at DESC
                LIMIT 5""",
            post.id,
            visible_list,
            post.score - score_range,
            post.score + score_range,
        )

    related = []
    for r in rows:
        post_obj = Post(id=r[0])
        related.append(
            {
                "pid": post_obj.get_external_id(),
                "title": r[1],
                "score": r[2],
                "comment_count": r[3],
            }
        )
    return Response.json({"related": related})


# ---------------------------------------------------------------------------
# Routes: Automod Rules (P3)
# ---------------------------------------------------------------------------


async def _fetch_automod_rules_data(forum_id: int) -> list[dict[str, str | int | bool]]:
    """Fetch automod rules for a forum. Shared by JSON and HTML routes."""
    rules = (
        await AutomodRule.objects.filter(forum_id=forum_id)
        .order_by("-created_at")
        .all()
    )
    return [
        {
            "id": r.id,
            "trigger": r.trigger if isinstance(r.trigger, str) else r.trigger.value,
            "condition": r.condition_json,
            "action": r.action if isinstance(r.action, str) else r.action.value,
            "is_active": r.is_active,
        }
        for r in rules
    ]


@app.get("/f/{forum_name}/automod")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def automod_list(request, forum_name: str):
    """List automod rules for a forum (mod/admin only)."""
    access = request.guard.access
    rule_list = await _fetch_automod_rules_data(access.forum.id)
    return Response.json({"rules": rule_list})


@app.get("/f/{forum_name}/automod/view")
@guard(
    *REQUIRE_LOGIN,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def automod_list_view(request, forum_name: str):
    """Render HTML automod management page for a forum (mod/admin only)."""
    access = request.guard.access
    rule_list = await _fetch_automod_rules_data(access.forum.id)
    ctx = build_context(
        request, forum=await _forum_to_dict(access.forum), rules=rule_list
    )
    return app.render("automod_manage.html", ctx)


@app.post("/f/{forum_name}/automod")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def automod_create(request, forum_name: str):
    """Create an automod rule (mod/admin only)."""
    access = request.guard.access
    forum = access.forum

    uid = get_uid(request)
    check_action_rate_limit(uid, "automod", 10, 60)

    try:
        data = await validate_form(request, AutomodCreateSchema)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))
    trigger_str = data.trigger
    action_str = data.action
    condition_str = data.condition

    try:
        trigger = AutomodTrigger(trigger_str)
    except ValueError:
        raise HTTPException(400, f"Invalid trigger: {trigger_str}")
    try:
        action = AutomodAction(action_str)
    except ValueError:
        raise HTTPException(400, f"Invalid action: {action_str}")

    # Validate condition JSON via schema (BaseModel auto-validates types + rejects unknown keys)
    try:
        validated = AutomodConditionSchema.model_validate_json(condition_str)
        condition_str = validated.model_dump_json(exclude_none=True)
    except Exception as exc:
        raise HTTPException(400, f"Invalid condition: {exc}")

    rule = AutomodRule(
        forum_id=forum.id,
        trigger=trigger,
        condition_json=condition_str,
        action=action,
        created_by=uid,
    )
    await rule.save()
    await _log_forum_action(
        uid,
        forum.id,
        "automod_create",
        reason=f"trigger={trigger_str} action={action_str}",
    )
    return Response.json({"ok": True, "id": rule.id})


@app.post("/f/{forum_name}/automod/{rule_id}/toggle")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def automod_toggle(request, forum_name: str, rule_id: int):
    """Enable/disable an automod rule."""
    access = request.guard.access
    forum = access.forum

    rule = await AutomodRule.objects.filter(id=rule_id, forum_id=forum.id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")

    new_state = not rule.is_active
    await AutomodRule.objects.filter(id=rule.id).update(is_active=new_state)

    uid = get_uid(request)
    await _log_forum_action(
        uid,
        forum.id,
        "automod_toggle",
        reason=f"rule {rule_id} {'enabled' if new_state else 'disabled'}",
    )
    return Response.json({"ok": True, "is_active": new_state})


@app.post("/f/{forum_name}/automod/{rule_id}/delete")
@guard(
    *REQUIRE_ACTIVE,
    Require.resource("access", resolver=_resolve_forum_admin, from_path="forum_name"),
)
async def automod_delete(request, forum_name: str, rule_id: int):
    """Delete an automod rule."""
    access = request.guard.access
    forum = access.forum

    rule = await AutomodRule.objects.filter(id=rule_id, forum_id=forum.id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")

    await AutomodRule.objects.filter(id=rule.id).delete()

    uid = get_uid(request)
    await _log_forum_action(
        uid, forum.id, "automod_delete", reason=f"rule {rule_id} deleted"
    )
    return Response.json({"ok": True})


async def _run_automod(
    db,
    forum_id: int,
    trigger: AutomodTrigger,
    post: Post | None = None,
    comment: Comment | None = None,
    user: User | None = None,
) -> list[str]:
    """Execute automod rules for a forum event. Returns list of actions taken."""
    rules = await AutomodRule.objects.filter(
        forum_id=forum_id,
        trigger=trigger.value,
        is_active=True,
    ).all()
    actions_taken = []

    for rule in rules:
        try:
            condition = AutomodConditionSchema.model_validate_json(rule.condition_json)
        except Exception as exc:
            logger.warning(
                "Automod rule {rid}: invalid condition JSON: {err}",
                rid=rule.id,
                err=str(exc),
            )
            continue

        # Evaluate conditions against validated schema fields
        matched = True
        if condition.min_karma is not None:
            # Automod triggers ONLY if user karma is BELOW threshold (spam from low-karma users)
            user_karma = user.karma if user else 0
            if user_karma >= condition.min_karma:
                matched = False
        if condition.contains_words is not None:
            text = ""
            if post:
                text = (post.title + " " + post.text).lower()
            elif comment:
                text = comment.text.lower()
            if not any(w.lower() in text for w in condition.contains_words):
                matched = False
        if condition.link_count_gt is not None and post:
            link_count = post.text.count("http://") + post.text.count("https://")
            if post.url:
                link_count += 1
            if link_count <= condition.link_count_gt:
                matched = False

        if not matched:
            continue

        # Execute action
        action_val = rule.action if isinstance(rule.action, str) else rule.action.value
        target_user_id = user.id if user else 0
        if action_val == AutomodAction.REMOVE.value:
            if post:
                async with db.transaction():
                    await Post.objects.filter(id=post.id).update(is_deleted=True)
                    if post.forum_id:
                        await Forum.objects.filter(id=post.forum_id).update(
                            post_count=F("post_count") - 1
                        )
                action_desc = f"removed post {post.id}"
            elif comment:
                await Comment.objects.filter(id=comment.id).update(is_deleted=True)
                action_desc = f"removed comment {comment.id}"
            else:
                continue
            actions_taken.append(action_desc)
            await _log_forum_action(
                0,
                forum_id,
                f"automod_{action_val}",
                reason=f"Rule {rule.id}: {action_desc}",
                target_user_id=target_user_id,
            )
        elif action_val == AutomodAction.FLAG.value:
            target_post_id = post.id if post else 0
            target_comment_id = comment.id if comment else 0
            await SpamReport(
                reporter_id=0,  # System
                post_id=target_post_id,
                comment_id=target_comment_id,
                reason=f"automod: rule {rule.id}",
            ).save()
            action_desc = "flagged for review"
            actions_taken.append(action_desc)
            await _log_forum_action(
                0,
                forum_id,
                f"automod_{action_val}",
                reason=f"Rule {rule.id}: {action_desc}",
                target_user_id=target_user_id,
            )
        elif action_val == AutomodAction.NOTIFY_MODS.value:
            # Notify mods/admins only — SQL-level filter
            mods = await ForumMember.objects.filter(
                forum_id=forum_id,
                role__in=[ForumRole.MODERATOR.value, ForumRole.ADMIN.value],
            ).all()
            msg = f"Automod triggered on {'post' if post else 'comment'}"
            for m in mods:
                await _notify(
                    m.user_id,
                    NotificationType.SYSTEM,
                    msg,
                    post_id=post.id if post else 0,
                    comment_id=comment.id if comment else 0,
                )
            action_desc = f"notified {len(mods)} mods"
            actions_taken.append(action_desc)
            await _log_forum_action(
                0,
                forum_id,
                f"automod_{action_val}",
                reason=f"Rule {rule.id}: {action_desc}",
                target_user_id=target_user_id,
            )

    if actions_taken:
        _cache.clear()
    return actions_taken


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


app.mount_health()
app.mount_version()


@app.get("/robots.txt")
async def robots_txt(request):
    """Crawler rules."""
    body = "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /account\nDisallow: /messages\n"
    return Response(body=body.encode(), content_type="text/plain")


@app.get("/.well-known/security.txt")
async def security_txt(request):
    """Security contact information (RFC 9116)."""
    body = "Contact: security@example.com\nPreferred-Languages: en\n"
    return Response(body=body.encode(), content_type="text/plain")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _port = int(_sys.argv[1]) if len(_sys.argv) > 1 else get_setting("PORT", 8000)
    app.run(port=_port)
