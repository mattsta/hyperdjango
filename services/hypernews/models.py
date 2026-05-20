"""
HyperNews models — HackerNews/Reddit clone with multi-forum architecture.

Models with PostgreSQL-backed storage, prefixed tables to avoid conflicts.
IDMixin on Post and User for opaque external IDs (enumeration-resistant; still authorize every access — not IDOR protection on their own).

Forum model enables subreddit-style communities — each forum has its own posts,
rules, moderators, and settings. Posts are scoped to forums via forum_id FK.

Data-access classmethods on Comment and ForumMember encapsulate the
cross-table SELECTs that the user_profile handler needs (and other
handlers can reuse). Raw SQL stays out of request handlers.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import Enum

from hyperdjango.database import get_db
from hyperdjango.fields import SlugField, create_field
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.public_id import IDMixin, IDMode, KeySlot
from hyperdjango.timeline import StatusTimelineMixin


class ReportStatus(Enum):
    PENDING = "pending"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"
    ACTIONED = "actioned"


class ForumRole(Enum):
    SUBSCRIBER = "subscriber"
    MODERATOR = "moderator"
    ADMIN = "admin"


class NotificationType(Enum):
    REPLY = "reply"
    MENTION = "mention"
    MOD_MESSAGE = "mod_message"
    SYSTEM = "system"


class PostStatus(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class AwardType(Enum):
    INSIGHTFUL = "insightful"
    WELL_WRITTEN = "well_written"
    HELPFUL = "helpful"
    FUNNY = "funny"


class AutomodTrigger(Enum):
    NEW_POST = "new_post"
    NEW_COMMENT = "new_comment"
    REPORT_THRESHOLD = "report_threshold"


class AutomodAction(Enum):
    REMOVE = "remove"
    FLAG = "flag"
    NOTIFY_MODS = "notify_mods"


class PollType(Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"


class User(StatusTimelineMixin, TimestampMixin):
    """User with timeline-based moderation and access control.

    Status categories:
    - moderation: banned, muted, warned — enforced via Require.not_banned() / not_muted()
    - access: staff, moderator — enforced via Require.has_active_status("access", "staff")
    """

    class Meta:
        table = "hn_users"

    class TimelineConfig:
        entity_type = "user"
        categories = {
            "moderation": ["banned", "muted", "warned"],
            "access": ["staff", "moderator"],
        }

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    email: str = Field(default="")
    password_hash: str = Field(exclude=True)
    display_name: str = Field(default="")
    bio: str = Field(default="")
    karma: int = Field(default=0)


class Forum(StatusTimelineMixin, TimestampMixin):
    """A community forum (subreddit-style). Each forum has its own posts, rules, and mods.

    Status categories (separate so forums can be archived AND hidden simultaneously):
    - archive: archived — read-only, browsable
    - lock: locked — no new posts, comments still visible
    - visibility: hidden — invisible in directory, only via direct URL
    is_public stays as a model field (structural config, not temporal status).
    """

    class Meta:
        table = "hn_forums"

    class TimelineConfig:
        entity_type = "forum"
        categories = {
            "archive": ["archived"],
            "lock": ["locked"],
            "visibility": ["hidden"],
        }

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)  # URL-safe slug: "python", "rust", "startups"
    title: str = Field()  # Display title: "Python Programming"
    description: str = Field(default="")  # Sidebar text (markdown)
    rules: str = Field(default="")  # Community rules (rendered in sidebar)
    is_public: bool = Field(
        default=True
    )  # Private forums require membership (structural, not temporal)
    created_by: int = Field(foreign_key=User)
    subscriber_count: int = Field(default=0)  # Denormalized, updated on join/leave
    post_count: int = Field(default=0)  # Denormalized


class ForumMember(TimestampMixin, Model):
    """Forum membership — tracks who belongs to which forum and their role."""

    class Meta:
        table = "hn_forum_members"
        indexes = [
            Index(fields=("forum_id", "user_id"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    forum_id: int = Field(foreign_key=Forum)
    user_id: int = Field(foreign_key=User)
    role: ForumRole = Field(default=ForumRole.SUBSCRIBER)
    joined_at: datetime = Field(default="now()")

    @classmethod
    async def public_memberships_for_user(
        cls, user_id: int, limit: int = 20
    ) -> list[tuple[str, str, str]]:
        """Public, non-hidden forum memberships for a user.

        Excludes forums currently in the 'hidden' status timeline state.
        Joins ForumMember → Forum and a NOT EXISTS check on
        ``hyper_status_events`` for active 'hidden' status. Returns
        ``(forum_name, forum_title, role)`` tuples ordered by name.
        """
        db = get_db()
        return await db.query_tuples(
            """SELECT f.name, f.title, fm.role
               FROM hn_forum_members fm JOIN hn_forums f ON f.id = fm.forum_id
               WHERE fm.user_id = $1 AND f.is_public = true
               AND NOT EXISTS (
                   SELECT 1 FROM hyper_status_events e
                   WHERE e.entity_type = 'forum' AND e.entity_id = f.id
                   AND e.status = 'hidden' AND e.ended_at IS NULL
                   AND (e.expires_at IS NULL OR e.expires_at > now())
               )
               ORDER BY f.name LIMIT $2""",
            user_id,
            limit,
        )


class Post(IDMixin, TimestampMixin, Model):
    class Meta:
        table = "hn_posts"
        indexes = [
            Index(fields=("forum_id", "-hot_score", "-id"), where="NOT is_deleted"),
            Index(fields=("forum_id", "-created_at", "-id"), where="NOT is_deleted"),
            Index(
                fields=("crosspost_source_id", "forum_id"),
                where="crosspost_source_id > 0 AND NOT is_deleted",
            ),
            Index(fields=("author_id", "-created_at"), where="NOT is_deleted"),
            Index(fields=("-hot_score",), where="NOT is_deleted"),
            Index(fields=("-controversy",), where="NOT is_deleted"),
            Index(fields=("-velocity",), where="NOT is_deleted"),
            Index(fields=("-score",)),
            Index(fields=("slug",)),
            Index(
                expressions=(
                    "(to_tsvector('english', title) || to_tsvector('english', COALESCE(text, '')))",
                ),
                using="gin",
                name="idx_hn_posts_search",
            ),
        ]

    class IDConfig:
        mode = IDMode.SIGNED
        alphabet = "CQ2VFx34f79pPr8hMcWjv5HgGwJRqX6m"
        hmac_keys = [KeySlot(key="hn-posts-key-2026-q1", offset=10000)]

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    slug: str = create_field(SlugField())
    url: str = Field(default="")
    text: str = Field(default="")
    author_id: int = Field(foreign_key=User)
    forum_id: int = Field(default=0)  # 0 = no forum (global); otherwise FK to hn_forums
    score: int = Field(default=1)
    weighted_score: float = Field(default=0.0)
    upvotes: int = Field(default=0)
    downvotes: int = Field(default=0)
    hot_score: float = Field(default=0.0)
    controversy: float = Field(default=0.0)
    velocity: float = Field(default=0.0)
    comment_count: int = Field(default=0)
    status: PostStatus = Field(default=PostStatus.PUBLISHED)
    crosspost_source_id: int = Field(default=0)  # Original post ID if crossposted
    is_pinned: bool = Field(default=False)
    pinned_by: int = Field(default=0)
    agree_count: int = Field(default=0)
    disagree_count: int = Field(default=0)
    is_ask: bool = Field(default=False)
    is_show: bool = Field(default=False)
    is_deleted: bool = Field(default=False)


class Comment(TimestampMixin, Model):
    class Meta:
        table = "hn_comments"
        indexes = [
            Index(fields=("post_id", "created_at")),
        ]

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(foreign_key=Post)
    author_id: int = Field(foreign_key=User)
    parent_id: int = Field(default=0)
    depth: int = Field(default=0)
    text: str = Field()
    score: int = Field(default=1)
    weighted_score: float = Field(default=0.0)
    upvotes: int = Field(default=0)
    downvotes: int = Field(default=0)
    agree_count: int = Field(default=0)
    disagree_count: int = Field(default=0)
    is_deleted: bool = Field(default=False)

    @classmethod
    async def recent_by_author_in_forums(
        cls,
        author_id: int,
        forum_ids: Sequence[int],
        limit: int = 20,
    ) -> list[tuple[int, int, str, int, datetime]]:
        """Recent non-deleted comments by an author, scoped to a set of
        visible forums.

        Joins through the parent post so we can filter on the post's
        ``forum_id`` — the cross-table predicate would otherwise require
        two ORM queries (fetch comments → fetch posts → filter).
        Returns ``(id, post_id, text, score, created_at)`` tuples
        ordered newest-first.
        """
        db = get_db()
        return await db.query_tuples(
            """SELECT c.id, c.post_id, c.text, c.score, c.created_at
               FROM hn_comments c JOIN hn_posts p ON p.id = c.post_id
               WHERE c.author_id = $1 AND NOT c.is_deleted
                 AND p.forum_id = ANY($2::int[])
               ORDER BY c.created_at DESC LIMIT $3""",
            author_id,
            list(forum_ids),
            limit,
        )


class Vote(TimestampMixin, Model):
    class Meta:
        table = "hn_votes"
        indexes = [
            Index(fields=("user_id", "post_id"), unique=True, where="post_id > 0"),
            Index(
                fields=("user_id", "comment_id"), unique=True, where="comment_id > 0"
            ),
        ]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    value: int = Field(default=1)


class AdminMessage(TimestampMixin, Model):
    class Meta:
        table = "hn_admin_messages"
        indexes = [
            Index(fields=("to_user_id",)),
        ]

    id: int = Field(primary_key=True, auto=True)
    from_user_id: int = Field(foreign_key=User)
    to_user_id: int = Field(foreign_key=User)
    subject: str = Field()
    body: str = Field()
    is_read: bool = Field(default=False)


class SpamReport(TimestampMixin, Model):
    class Meta:
        table = "hn_spam_reports"
        indexes = [
            Index(
                fields=("reporter_id", "post_id", "comment_id"),
                unique=True,
                where="post_id > 0 OR comment_id > 0",
            ),
        ]

    id: int = Field(primary_key=True, auto=True)
    reporter_id: int = Field(foreign_key=User)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    reason: str = Field(default="")
    status: ReportStatus = Field(default=ReportStatus.PENDING)
    reviewed_by_id: int = Field(default=0)


class Bookmark(TimestampMixin, Model):
    """Saved posts and comments. One per user+post or user+comment."""

    class Meta:
        table = "hn_bookmarks"
        indexes = [
            Index(fields=("user_id", "post_id"), unique=True, where="post_id > 0"),
            Index(
                fields=("user_id", "comment_id"), unique=True, where="comment_id > 0"
            ),
        ]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)


class Notification(TimestampMixin, Model):
    """User notifications — replies, mentions, mod messages, system alerts."""

    class Meta:
        table = "hn_notifications"
        indexes = [
            Index(fields=("user_id", "is_read", "-created_at")),
        ]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    type: NotificationType = Field(default=NotificationType.SYSTEM)
    actor_id: int = Field(default=0)  # Who triggered it (0 = system)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    message: str = Field(default="")
    is_read: bool = Field(default=False)


class UserProfile(TimestampMixin, Model):
    """Extended user profile — website, location, avatar, etc."""

    class Meta:
        table = "hn_user_profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(unique=True, foreign_key=User)
    website: str = Field(default="")
    location: str = Field(default="")
    avatar_url: str = Field(default="")
    github_username: str = Field(default="")


class PostRevision(TimestampMixin, Model):
    """Edit history for posts — every edit creates a new revision."""

    class Meta:
        table = "hn_post_revisions"
        indexes = [
            Index(fields=("post_id", "-created_at")),
        ]

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(foreign_key=Post)
    title: str = Field()
    text: str = Field(default="")
    edited_by: int = Field(foreign_key=User)
    edit_reason: str = Field(default="")


class Poll(TimestampMixin, Model):
    """Poll attached to a post — one poll per post."""

    class Meta:
        table = "hn_polls"

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(unique=True, foreign_key=Post)
    question: str = Field()
    poll_type: PollType = Field(default=PollType.SINGLE_CHOICE)
    closes_at: str = Field(default="")  # ISO timestamp or "" for open-ended
    allow_add_options: bool = Field(default=False)


class PollOption(TimestampMixin, Model):
    """A choice in a poll."""

    class Meta:
        table = "hn_poll_options"

    id: int = Field(primary_key=True, auto=True)
    poll_id: int = Field(foreign_key=Poll)
    text: str = Field()
    position: int = Field(default=0)
    vote_count: int = Field(default=0)


class PollVote(TimestampMixin, Model):
    """A user's vote on a poll option."""

    class Meta:
        table = "hn_poll_votes"
        indexes = [
            Index(fields=("poll_id", "user_id", "option_id"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    poll_id: int = Field(foreign_key=Poll)
    option_id: int = Field(foreign_key=PollOption)
    user_id: int = Field(foreign_key=User)


class UserFlair(TimestampMixin, Model):
    """Per-forum identity marker — assigned by mods or self-assigned."""

    class Meta:
        table = "hn_user_flairs"
        indexes = [
            Index(fields=("user_id", "forum_id"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    forum_id: int = Field(foreign_key=Forum)
    text: str = Field(default="")
    css_class: str = Field(default="custom")  # mod, admin, verified, custom
    assigned_by: int = Field(default=0)  # 0 = self-assigned


class Award(TimestampMixin, Model):
    """Community recognition — karma-gated badges on posts/comments."""

    class Meta:
        table = "hn_awards"
        indexes = [
            Index(fields=("post_id",), where="post_id > 0"),
            Index(fields=("comment_id",), where="comment_id > 0"),
        ]

    id: int = Field(primary_key=True, auto=True)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    user_id: int = Field(foreign_key=User)  # Who gave it
    award_type: AwardType = Field(default=AwardType.INSIGHTFUL)


class Sequence(TimestampMixin, Model):
    """Ordered collection of posts — LessWrong-style series."""

    class Meta:
        table = "hn_sequences"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    description: str = Field(default="")
    author_id: int = Field(foreign_key=User)
    is_public: bool = Field(default=True)


class SequenceEntry(TimestampMixin, Model):
    """A post's position in a sequence."""

    class Meta:
        table = "hn_sequence_entries"
        indexes = [
            Index(fields=("sequence_id", "post_id"), unique=True),
        ]

    id: int = Field(primary_key=True, auto=True)
    sequence_id: int = Field(foreign_key=Sequence)
    post_id: int = Field(foreign_key=Post)
    position: int = Field(default=0)


class AutomodRule(TimestampMixin, Model):
    """Per-forum automated moderation rule."""

    class Meta:
        table = "hn_automod_rules"
        indexes = [
            Index(fields=("forum_id", "is_active"), where="is_active"),
        ]

    id: int = Field(primary_key=True, auto=True)
    forum_id: int = Field(foreign_key=Forum)
    trigger: AutomodTrigger = Field(default=AutomodTrigger.NEW_POST)
    condition_json: str = Field(
        default="{}"
    )  # JSON: {"min_karma": 10, "contains_words": [...]}
    action: AutomodAction = Field(default=AutomodAction.FLAG)
    is_active: bool = Field(default=True)
    created_by: int = Field(foreign_key=User)
