"""
HyperNews Voting System — abuse prevention, trust tiers, social graph analytics.

Phase 1: Race condition fix, ban/mute enforcement, vote rate limiting,
         self-vote prevention, rapid-fire detection.
Phase 2: Time-decay ranking (hot_score, controversy, velocity).
Phase 3: Trust tiers + weighted votes.
Phase 4: Vote graph, social analytics, ring detection.
Phase 5: Meta-moderation (agree/disagree, tags, mod notes, auto-hide).
Phase 6: Eigenvector centrality, community detection, domain authority.
"""

from datetime import UTC, datetime
from datetime import timedelta as _timedelta
from enum import Enum

from hyperdjango import HTTPException
from hyperdjango.database import get_db
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Index, Model
from hyperdjango.ratelimit import InMemoryRateLimitBackend
from hyperdjango.security import SecurityEvent, SecurityLog

from .models import Comment, Post, User, Vote


class TargetType(Enum):
    POST = "post"
    COMMENT = "comment"


class Visibility(Enum):
    MOD_ONLY = "mod_only"
    AUTHOR_VISIBLE = "author_visible"
    PUBLIC = "public"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VOTE_RATE_LIMIT = 30  # max votes per window
VOTE_RATE_WINDOW = 60  # seconds
RAPID_FIRE_THRESHOLD = 10  # votes in RAPID_FIRE_WINDOW triggers flag
RAPID_FIRE_WINDOW = 10  # seconds
DOWNVOTE_MIN_ACCOUNT_AGE_DAYS = 7  # accounts must be 7+ days old to downvote
STAFF_VOTE_WEIGHT = 3.0  # minimum weight for staff votes


# ---------------------------------------------------------------------------
# Trust Tiers — karma-based privilege levels
# ---------------------------------------------------------------------------


class TrustTier(TimestampMixin, Model):
    class Meta:
        table = "hn_trust_tiers"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(unique=True)
    min_karma: int = Field()
    max_karma: int = Field(default=-1)
    vote_weight: float = Field(default=1.0)
    can_downvote: bool = Field(default=False)
    can_flag: bool = Field(default=False)
    can_meta_moderate: bool = Field(default=False)
    mod_power: float = Field(default=0.0)


# In-process tier cache — loaded once at startup, rarely changes
_tier_cache: list[TrustTier] | None = None


async def get_user_tier(karma: int) -> TrustTier:
    """Resolve user's trust tier from karma. Uses in-process cache."""
    global _tier_cache
    if _tier_cache is None:
        _tier_cache = list(await TrustTier.objects.order_by("min_karma").all())
    if not _tier_cache:
        # Fallback if tiers not seeded — return a default
        return TrustTier(id=0, name="default", min_karma=0, vote_weight=1.0)
    for tier in reversed(_tier_cache):
        if karma >= tier.min_karma:
            return tier
    return _tier_cache[0]


def invalidate_tier_cache() -> None:
    """Call when trust tiers are modified via admin."""
    global _tier_cache
    _tier_cache = None


async def check_downvote_permission(user: User, value: int) -> None:
    """Enforce downvote restrictions: tier + account age."""
    if value >= 0:
        return
    tier = await get_user_tier(user.karma)
    if not tier.can_downvote:
        raise HTTPException(
            403, f"Downvoting requires {100}+ karma (your tier: {tier.name})"
        )
    account_age = (datetime.now(UTC) - user.created_at).days
    if account_age < DOWNVOTE_MIN_ACCOUNT_AGE_DAYS:
        raise HTTPException(
            403,
            f"Account must be {DOWNVOTE_MIN_ACCOUNT_AGE_DAYS}+ days old to downvote",
        )


async def get_vote_weight(user: User) -> float:
    """Get the vote weight multiplier for this user based on tier + staff status."""
    tier = await get_user_tier(user.karma)
    weight = tier.vote_weight
    if await user.has_status("access", "staff"):
        weight = max(weight, STAFF_VOTE_WEIGHT)
    return weight


# ---------------------------------------------------------------------------
# Vote rate limiter (in-process, per-user)
# ---------------------------------------------------------------------------

_vote_limiter = InMemoryRateLimitBackend()

# ---------------------------------------------------------------------------
# Vote abuse checks
# ---------------------------------------------------------------------------


def check_vote_rate_limit(uid: int) -> None:
    """Per-user vote rate limit. Raises HTTPException(429) if exceeded."""
    _check_rate(f"vote:{uid}", VOTE_RATE_LIMIT, VOTE_RATE_WINDOW)


def check_action_rate_limit(uid: int, action: str, limit: int, window: int) -> None:
    """Per-user per-action rate limit. Raises HTTPException(429) if exceeded."""
    _check_rate(f"{action}:{uid}", limit, window)


def _check_rate(key: str, limit: int, window: int) -> None:
    """Check rate limit for key. Raises HTTPException(429) if exceeded."""
    allowed, remaining, reset = _vote_limiter.check_and_increment(key, limit, window)
    if not allowed:
        raise HTTPException(429, f"Too many requests. Try again in {reset}s.")


async def check_self_vote(db, uid: int, post_id: int, comment_id: int) -> None:
    """Prevent users from voting on their own content."""
    if post_id:
        post = await Post.objects.filter(id=post_id).first()
        if post is not None and post.author_id == uid:
            raise HTTPException(400, "Cannot vote on your own content")
    if comment_id:
        comment = await Comment.objects.filter(id=comment_id).first()
        if comment is not None and comment.author_id == uid:
            raise HTTPException(400, "Cannot vote on your own content")


async def log_rapid_fire(db, uid: int) -> None:
    """Insert rate log entry and check for rapid-fire voting."""
    rate_entry = VoteRateLog(user_id=uid)
    await rate_entry.save()
    cutoff = datetime.now(UTC) - _timedelta(seconds=10)
    rapid_count = await VoteRateLog.objects.filter(user_id=uid, ts__gt=cutoff).count()
    rapid = [(rapid_count,)]
    if rapid and rapid[0][0] > RAPID_FIRE_THRESHOLD:
        SecurityLog.log(
            SecurityEvent.SUSPICIOUS_INPUT,
            user_id=uid,
            detail=f"rapid-fire voting: {rapid[0][0]} votes in 10s",
        )
        raise HTTPException(429, "Voting too fast")


async def ensure_voting_tables(db) -> None:
    """Create voting infrastructure at startup.

    Tables are created by ``hyper setup --drop`` from model definitions.
    This function handles only what models can't express:
    - UNLOGGED tables (runtime analytics, not persisted across crash)
    - Custom composite/partial/GIN indexes
    - CHECK constraints
    - Trust tier seed data
    """
    # All indexes now defined in Meta.indexes on each Model class.
    # Only trust tier seeding remains here.

    # ── Seed trust tiers (idempotent) ─────────────────────────────────────
    existing_tiers = await TrustTier.objects.count()
    if existing_tiers == 0:
        for name, min_k, max_k, weight, down, flag, meta, power in [
            ("new", 0, 9, 1.0, False, False, False, 0.0),
            ("established", 10, 99, 1.2, False, True, False, 0.5),
            ("trusted", 100, 499, 1.5, True, True, True, 1.0),
            ("authority", 500, -1, 2.0, True, True, True, 2.0),
        ]:
            tier = TrustTier(
                name=name,
                min_karma=min_k,
                max_karma=max_k,
                vote_weight=weight,
                can_downvote=down,
                can_flag=flag,
                can_meta_moderate=meta,
                mod_power=power,
            )
            await tier.save()

    invalidate_tier_cache()

    # ── CHECK constraints (prevent negative values from bugs) ─────────────
    for tbl, col in [
        ("hn_posts", "upvotes"),
        ("hn_posts", "downvotes"),
        ("hn_comments", "upvotes"),
        ("hn_comments", "downvotes"),
    ]:
        constraint_name = f"chk_{tbl}_{col}_nonneg"
        await db.execute(f"""
            DO $$ BEGIN
                ALTER TABLE {tbl} ADD CONSTRAINT {constraint_name} CHECK ({col} >= 0);
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$
        """)


async def cleanup_old_data(db) -> None:
    """Clean up old analytics data and rate limiter entries. Called periodically."""
    # Vote events older than 180 days (graph analytics only uses 90 days)
    cutoff_180d = datetime.now(UTC) - _timedelta(days=180)
    await VoteEvent.objects.filter(created_at__lt=cutoff_180d).delete()
    # Rate log entries older than 5 minutes
    cutoff_5m = datetime.now(UTC) - _timedelta(minutes=5)
    await VoteRateLog.objects.filter(ts__lt=cutoff_5m).delete()


async def refresh_hot_scores(db) -> None:
    """Recompute hot_score, controversy, velocity for recent posts.

    HN gravity: score / (age_hours + 2) ^ 1.8
    Controversy: (up + down) / (|up - down| + 1) — higher = more balanced votes
    Velocity: votes in last 2 hours / age in hours — detects rising content

    Only recomputes posts from last 7 days (older posts have near-zero hot scores).
    """
    await db.execute("""
        UPDATE hn_posts SET
            hot_score = GREATEST(weighted_score, score)::float / POWER(GREATEST(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 + 2, 0.001), 1.8),
            controversy = CASE WHEN (upvotes + downvotes) >= 2
                THEN (upvotes + downvotes)::float / (ABS(upvotes - downvotes) + 1)
                ELSE 0 END,
            velocity = CASE WHEN EXTRACT(EPOCH FROM (NOW() - created_at)) > 0
                THEN GREATEST(weighted_score, score)::float / GREATEST(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0, 0.1)
                ELSE 0 END
        WHERE is_deleted = false AND created_at > NOW() - INTERVAL '7 days'
    """)


async def cleanup_rate_log(db) -> None:
    """Periodic cleanup of old rate log entries (call from background task)."""
    cutoff = datetime.now(UTC) - _timedelta(minutes=5)
    await VoteRateLog.objects.filter(ts__lt=cutoff).delete()


# ---------------------------------------------------------------------------
# Phase 4: Vote Graph — rich vote event tracking + social analytics
# ---------------------------------------------------------------------------


class VoteEvent(TimestampMixin, Model):
    """Rich vote audit trail for social graph analytics.

    Records who voted, what they voted on, who authored the target,
    the vote weight at time of casting, and the domain (for URL posts).
    Enables: user affinity graphs, domain authority, voting ring detection.
    """

    class Meta:
        table = "hn_vote_events"
        indexes = [
            Index(fields=("voter_id", "-created_at")),
            Index(fields=("voter_id", "target_author_id")),
            Index(fields=("target_author_id", "-created_at")),
            Index(fields=("domain",), where="domain != ''"),
        ]

    id: int = Field(primary_key=True, auto=True)
    voter_id: int = Field(foreign_key=User)
    target_type: TargetType = Field()
    target_id: int = Field()
    target_author_id: int = Field()
    value: int = Field()  # +1 or -1
    weight: float = Field(default=1.0)
    voter_karma_at_time: int = Field()
    domain: str = Field(default="")
    created_at: datetime = Field(default="now()")


async def record_vote_event(
    db,
    voter_id: int,
    target_type: str,
    target_id: int,
    target_author_id: int,
    value: int,
    weight: float,
    voter_karma: int,
    domain: str,
) -> None:
    """Insert a rich vote event into the analytics table."""
    event = VoteEvent(
        voter_id=voter_id,
        target_type=target_type,
        target_id=target_id,
        target_author_id=target_author_id,
        value=value,
        weight=weight,
        voter_karma_at_time=voter_karma,
        domain=domain,
    )
    await event.save()


async def query_user_affinity(db, user_id: int, limit: int = 20) -> list[tuple]:
    """Top users that user_id upvotes most frequently.

    Returns: [(target_author_id, vote_count, net_sentiment, avg_weight), ...]
    """
    return await db.query_tuples(
        """SELECT target_author_id, COUNT(*) AS vote_count,
                  SUM(value) AS net_sentiment, AVG(weight) AS avg_weight
           FROM hn_vote_events WHERE voter_id = $1
           GROUP BY target_author_id ORDER BY vote_count DESC LIMIT $2""",
        user_id,
        limit,
    )


async def query_domain_preferences(db, user_id: int, limit: int = 20) -> list[tuple]:
    """Top domains that user_id votes on.

    Returns: [(domain, total_votes, upvotes, downvotes), ...]
    """
    return await db.query_tuples(
        """SELECT domain, COUNT(*) AS total,
                  SUM(CASE WHEN value > 0 THEN 1 ELSE 0 END) AS ups,
                  SUM(CASE WHEN value < 0 THEN 1 ELSE 0 END) AS downs
           FROM hn_vote_events WHERE voter_id = $1 AND domain != ''
           GROUP BY domain ORDER BY ups DESC LIMIT $2""",
        user_id,
        limit,
    )


async def detect_voting_rings(db) -> list[tuple]:
    """Find mutual upvote pairs with high reciprocity (potential voting rings).

    Returns: [(user_a, user_b, a_votes_b, b_votes_a, reciprocity), ...]
    Reciprocity = min(a→b, b→a) / max(a→b, b→a) — 1.0 = perfectly mutual.
    """
    return await db.query_tuples("""
        WITH pair_counts AS (
            SELECT voter_id, target_author_id, COUNT(*) AS cnt
            FROM hn_vote_events
            WHERE value > 0 AND created_at > NOW() - INTERVAL '30 days'
            GROUP BY voter_id, target_author_id
            HAVING COUNT(*) >= 3
        ),
        mutual AS (
            SELECT a.voter_id AS user_a, a.target_author_id AS user_b,
                   a.cnt AS a_votes_b, b.cnt AS b_votes_a
            FROM pair_counts a
            JOIN pair_counts b ON a.voter_id = b.target_author_id
                              AND a.target_author_id = b.voter_id
            WHERE a.voter_id < a.target_author_id
        )
        SELECT user_a, user_b, a_votes_b, b_votes_a,
               LEAST(a_votes_b, b_votes_a)::float / GREATEST(a_votes_b, b_votes_a) AS reciprocity
        FROM mutual
        WHERE LEAST(a_votes_b, b_votes_a) >= 3
        ORDER BY (a_votes_b + b_votes_a) DESC
        LIMIT 50
    """)


async def query_domain_authority(db, limit: int = 30) -> list[tuple]:
    """Domain authority: domains most upvoted by high-karma users.

    Returns: [(domain, total_votes, weighted_upvotes, avg_voter_karma), ...]
    """
    return await db.query_tuples(
        """SELECT domain, COUNT(*) AS total,
                  SUM(CASE WHEN value > 0 THEN weight ELSE 0 END) AS weighted_ups,
                  AVG(voter_karma_at_time) FILTER (WHERE value > 0) AS avg_karma
           FROM hn_vote_events WHERE domain != '' AND created_at > NOW() - INTERVAL '30 days'
           GROUP BY domain HAVING COUNT(*) >= 3
           ORDER BY weighted_ups DESC LIMIT $1""",
        limit,
    )


async def run_ring_detection(db) -> None:
    """Run ring detection and log suspicious patterns to SecurityLog."""
    rings = await detect_voting_rings(db)
    for ring in rings:
        user_a, user_b, a_votes_b, b_votes_a, reciprocity = ring
        if reciprocity > 0.7:
            SecurityLog.log(
                SecurityEvent.SUSPICIOUS_INPUT,
                detail=f"Voting ring: users {user_a}↔{user_b}, "
                f"mutual votes {a_votes_b}/{b_votes_a}, reciprocity {reciprocity:.2f}",
            )


# ---------------------------------------------------------------------------
# Phase 5: Meta-Moderation (LessWrong-inspired)
# ---------------------------------------------------------------------------

# Valid content quality tags
VALID_CONTENT_TAGS = frozenset(
    {
        "insightful",
        "needs-citation",
        "off-topic",
        "personal-attack",
        "factual-error",
        "spam",
        "duplicate",
    }
)

# Tags that trigger auto-hide when threshold reached
FLAG_TAGS = frozenset({"off-topic", "personal-attack", "spam"})
FLAG_HIDE_THRESHOLD = 3  # 3+ flag-type tags → auto-hide


class AgreementVote(TimestampMixin, Model):
    """Agree/disagree axis — separate from quality votes (LessWrong-style)."""

    class Meta:
        table = "hn_agreement_votes"
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
    value: int = Field()  # +1 agree, -1 disagree
    created_at: datetime = Field(default="now()")


class ContentTag(TimestampMixin, Model):
    """Quality tags on content: insightful, off-topic, spam, etc."""

    class Meta:
        table = "hn_content_tags"
        indexes = [
            Index(fields=("post_id", "comment_id", "tag")),
        ]

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    tag: str = Field()
    created_at: datetime = Field(default="now()")


class ModNote(TimestampMixin, Model):
    """Moderator notes on content or users."""

    class Meta:
        table = "hn_mod_notes"
        indexes = [
            Index(fields=("target_user_id",)),
        ]

    id: int = Field(primary_key=True, auto=True)
    moderator_id: int = Field(foreign_key=User)
    target_user_id: int = Field(default=0)
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    note: str = Field()
    visibility: Visibility = Field(default=Visibility.MOD_ONLY)
    created_at: datetime = Field(default="now()")


class ModAction(TimestampMixin, Model):
    """Moderation action log for transparency.

    Covers both user-level actions (ban, mute) and forum-level actions
    (archive, lock, delete, transfer_admin, appoint_mod, remove_mod).
    """

    class Meta:
        table = "hn_mod_actions"
        indexes = [
            Index(fields=("target_user_id", "-created_at")),
            Index(fields=("forum_id", "-created_at"), where="forum_id > 0"),
        ]

    id: int = Field(primary_key=True, auto=True)
    moderator_id: int = Field(foreign_key=User)
    target_user_id: int = Field(default=0)
    action: str = Field()
    reason: str = Field(default="")
    post_id: int = Field(default=0)
    comment_id: int = Field(default=0)
    forum_id: int = Field(default=0)
    created_at: datetime = Field(default="now()")


class VoteRateLog(Model):
    """Rate limiting log for votes. UNLOGGED — lost on crash, fast writes."""

    class Meta:
        table = "hn_vote_rate_log"
        unlogged = True
        indexes = [
            Index(fields=("user_id", "-ts")),
        ]

    user_id: int = Field()
    ts: datetime = Field(default="now()")


class UserAffinity(Model):
    """Pairwise vote sentiment between users. UNLOGGED — recomputed from vote events."""

    class Meta:
        table = "hn_user_affinity"
        unlogged = True

    user_a: int = Field(primary_key=True)
    user_b: int = Field(primary_key=True)
    upvote_count: int = Field(default=0)
    downvote_count: int = Field(default=0)
    net_sentiment: int = Field(default=0)


class UserCentrality(Model):
    """Eigenvector centrality scores. UNLOGGED — recomputed from affinity graph."""

    class Meta:
        table = "hn_user_centrality"
        unlogged = True

    user_id: int = Field(primary_key=True)
    score: float = Field(default=1.0)
    prev_score: float = Field(default=0.0)
    iteration: int = Field(default=0)


class UserCommunity(Model):
    """Community detection assignments. UNLOGGED — recomputed from affinity graph."""

    class Meta:
        table = "hn_user_community"
        unlogged = True

    user_id: int = Field(primary_key=True)
    community_id: int = Field()
    iteration: int = Field(default=0)


async def apply_content_tag(
    db, user: User, post_id: int, comment_id: int, tag: str
) -> None:
    """Apply a content quality tag. Checks tier permission and triggers auto-hide."""
    if tag not in VALID_CONTENT_TAGS:
        raise HTTPException(400, f"Invalid tag: {tag}")

    tier = await get_user_tier(user.karma)
    if not tier.can_flag:
        raise HTTPException(403, "Tagging requires 'established' tier (10+ karma)")

    # Check if tag already exists for this user+target (ORM-level dedup)
    existing = await ContentTag.objects.filter(
        user_id=user.id,
        post_id=post_id,
        comment_id=comment_id,
        tag=tag,
    ).first()
    if not existing:
        ct = ContentTag(
            user_id=user.id, post_id=post_id, comment_id=comment_id, tag=tag
        )
        await ct.save()

    # Auto-hide check: count flag-type tags on this content
    if tag in FLAG_TAGS:
        await check_auto_hide(post_id=post_id, comment_id=comment_id)


async def cleanup_orphaned_data(db, post_id: int = 0, comment_id: int = 0) -> None:
    """Clean up votes, events, tags, agreements when content is soft-deleted."""
    if post_id:
        await Vote.objects.filter(post_id=post_id).delete()
        await AgreementVote.objects.filter(post_id=post_id).delete()
        await ContentTag.objects.filter(post_id=post_id).delete()
        await VoteEvent.objects.filter(
            target_type=TargetType.POST.value, target_id=post_id
        ).delete()
    if comment_id:
        await Vote.objects.filter(comment_id=comment_id).delete()
        await AgreementVote.objects.filter(comment_id=comment_id).delete()
        await ContentTag.objects.filter(comment_id=comment_id).delete()
        await VoteEvent.objects.filter(
            target_type=TargetType.COMMENT.value, target_id=comment_id
        ).delete()


async def check_auto_hide(post_id: int = 0, comment_id: int = 0) -> None:
    """Auto-hide content when flag-type tags exceed threshold."""
    db = get_db()

    if post_id:
        flag_count = await ContentTag.objects.filter(
            post_id=post_id,
            tag__in=list(FLAG_TAGS),
        ).count()
        if flag_count >= FLAG_HIDE_THRESHOLD:
            await Post.objects.filter(id=post_id).update(is_deleted=True)
            await cleanup_orphaned_data(db, post_id=post_id)
            action = ModAction(
                moderator_id=0,
                action="auto_hide",
                post_id=post_id,
                reason=f"Auto-hidden: {flag_count} flags",
            )
            await action.save()

    if comment_id:
        flag_count = await ContentTag.objects.filter(
            comment_id=comment_id,
            tag__in=list(FLAG_TAGS),
        ).count()
        if flag_count >= FLAG_HIDE_THRESHOLD:
            await Comment.objects.filter(id=comment_id).update(is_deleted=True)
            await cleanup_orphaned_data(db, comment_id=comment_id)
            action = ModAction(
                moderator_id=0,
                action="auto_hide",
                comment_id=comment_id,
                reason=f"Auto-hidden: {flag_count} flags",
            )
            await action.save()


# ---------------------------------------------------------------------------
# Phase 6: Eigenvector Centrality + Community Detection + Domain Authority
# ---------------------------------------------------------------------------

CENTRALITY_ITERATIONS = 15
CENTRALITY_CONVERGENCE = 0.001
COMMUNITY_ITERATIONS = 10


async def refresh_user_affinity(db) -> None:
    """Refresh the UNLOGGED user affinity matrix from vote events (last 90 days).

    Aggregates voter→author pairs with upvote/downvote counts and net sentiment.
    """
    await db.execute("DELETE FROM hn_user_affinity")
    await db.execute("""
        INSERT INTO hn_user_affinity (user_a, user_b, upvote_count, downvote_count, net_sentiment)
        SELECT voter_id, target_author_id,
               SUM(CASE WHEN value > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN value < 0 THEN 1 ELSE 0 END),
               SUM(value)
        FROM hn_vote_events
        WHERE created_at > NOW() - INTERVAL '90 days'
        GROUP BY voter_id, target_author_id
        HAVING COUNT(*) >= 2
    """)


async def compute_eigenvector_centrality(db) -> None:
    """Compute eigenvector centrality via power iteration (pure SQL).

    Each user's score = normalized sum of (neighbor_score * edge_weight)
    for all incoming positive edges. Converges in ~10-15 iterations.
    """
    # Initialize all active users with score 1.0
    await db.execute("DELETE FROM hn_user_centrality")
    await db.execute("""
        INSERT INTO hn_user_centrality (user_id, score, prev_score)
        SELECT DISTINCT id, 1.0, 0.0 FROM hn_users u
        WHERE NOT EXISTS (
            SELECT 1 FROM hyper_status_events e
            WHERE e.entity_type = 'user' AND e.entity_id = u.id
            AND e.status = 'banned' AND e.ended_at IS NULL
            AND (e.expires_at IS NULL OR e.expires_at > now())
        )
    """)

    for i in range(CENTRALITY_ITERATIONS):
        # One power iteration: new_score = Σ(neighbor_score × edge_weight) / L2_norm
        await db.execute(
            """
            WITH incoming AS (
                SELECT a.user_b AS user_id,
                       SUM(c.score * a.net_sentiment::float
                           / GREATEST(a.upvote_count + a.downvote_count, 1))
                       AS weighted_sum
                FROM hn_user_affinity a
                JOIN hn_user_centrality c ON c.user_id = a.user_a
                WHERE a.net_sentiment > 0
                GROUP BY a.user_b
            ),
            norm AS (
                SELECT GREATEST(SQRT(SUM(weighted_sum * weighted_sum)), 1e-10) AS l2
                FROM incoming
            )
            UPDATE hn_user_centrality uc
            SET prev_score = uc.score,
                score = COALESCE(i.weighted_sum / n.l2, 0.001),
                iteration = $1
            FROM incoming i, norm n
            WHERE uc.user_id = i.user_id
        """,
            i + 1,
        )

        # Check convergence
        delta = await db.query_tuples(
            "SELECT MAX(ABS(score - prev_score)) FROM hn_user_centrality"
        )
        if delta and delta[0][0] is not None and delta[0][0] < CENTRALITY_CONVERGENCE:
            break


async def compute_community_detection(db) -> None:
    """Community detection via label propagation (pure SQL).

    Each user starts as their own community. Iteratively adopts the
    most common label among positively-connected neighbors.
    """
    # Initialize: each user is their own community
    await db.execute("DELETE FROM hn_user_community")
    await db.execute("""
        INSERT INTO hn_user_community (user_id, community_id)
        SELECT id, id FROM hn_users u
        WHERE NOT EXISTS (
            SELECT 1 FROM hyper_status_events e
            WHERE e.entity_type = 'user' AND e.entity_id = u.id
            AND e.status = 'banned' AND e.ended_at IS NULL
            AND (e.expires_at IS NULL OR e.expires_at > now())
        )
    """)

    for i in range(COMMUNITY_ITERATIONS):
        # One iteration: adopt most common neighbor label
        changed = await db.execute(
            """
            WITH neighbor_labels AS (
                SELECT a.user_a AS user_id,
                       c.community_id AS neighbor_community,
                       SUM(a.net_sentiment) AS weight
                FROM hn_user_affinity a
                JOIN hn_user_community c ON c.user_id = a.user_b
                WHERE a.net_sentiment > 0
                GROUP BY a.user_a, c.community_id
            ),
            best_label AS (
                SELECT DISTINCT ON (user_id) user_id, neighbor_community
                FROM neighbor_labels
                ORDER BY user_id, weight DESC
            )
            UPDATE hn_user_community uc
            SET community_id = bl.neighbor_community,
                iteration = $1
            FROM best_label bl
            WHERE uc.user_id = bl.user_id AND uc.community_id != bl.neighbor_community
        """,
            i + 1,
        )
        # db.execute() returns the affected-row count; zero rows changed = converged
        if changed == 0:
            break


async def query_communities(db, limit: int = 20) -> list[tuple]:
    """Get community clusters with member counts.

    Returns: [(community_id, member_count, member_usernames), ...]
    """
    return await db.query_tuples(
        """
        SELECT uc.community_id, COUNT(*) AS members,
               ARRAY_AGG(u.username ORDER BY u.karma DESC) AS usernames
        FROM hn_user_community uc
        JOIN hn_users u ON u.id = uc.user_id
        GROUP BY uc.community_id
        HAVING COUNT(*) >= 2
        ORDER BY members DESC
        LIMIT $1
    """,
        limit,
    )


async def query_centrality_leaders(db, limit: int = 20) -> list[tuple]:
    """Get users with highest eigenvector centrality.

    Returns: [(user_id, username, centrality_score, karma), ...]
    """
    return await db.query_tuples(
        """
        SELECT uc.user_id, u.username, uc.score, u.karma
        FROM hn_user_centrality uc
        JOIN hn_users u ON u.id = uc.user_id
        ORDER BY uc.score DESC
        LIMIT $1
    """,
        limit,
    )


async def run_graph_analytics(db) -> None:
    """Full graph analytics pipeline: affinity → centrality → communities.

    Called periodically (daily) from the background refresh thread.
    """
    await refresh_user_affinity(db)
    await compute_eigenvector_centrality(db)
    await compute_community_detection(db)
