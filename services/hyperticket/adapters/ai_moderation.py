"""
AI Content Moderation Adapter — screen customer-submitted content.

Demo adapter that checks for policy violations in comments.
In production, this would call a moderation API (OpenAI, Perspective, etc.).

Demonstrates the adapter pipeline's on_comment_pre hook, which can
modify or flag content before it's saved.
"""

from hyperdjango.logging import logger

from .protocols import AdapterContext

# Words that trigger flagging in demo mode
_FLAGGED_PATTERNS = frozenset(
    {
        "spam",
        "scam",
        "phishing",
        "malware",
    }
)


class AIContentModerationAdapter:
    """Screen customer content for policy violations (demo keyword matching)."""

    async def on_comment_pre(self, ctx: AdapterContext, ticket, data: dict) -> dict:
        """Pre-hook: flag suspicious content from customers."""
        if ctx.actor_type != "customer":
            return data  # Only moderate customer content

        body = (data.get("body") or "").lower()
        for pattern in _FLAGGED_PATTERNS:
            if pattern in body:
                data["body"] = f"[Flagged for review] {data.get('body', '')}"
                ctx.metadata["content_flagged"] = True
                logger.warning(
                    "Content moderation: flagged comment on ticket {tid} by customer {cid}",
                    tid=ticket.id,
                    cid=ctx.actor_id,
                )
                break

        return data

    async def on_comment_post(self, ctx: AdapterContext, ticket, comment) -> None:
        """Post-hook: log moderation decision if flagged."""
        if ctx.metadata.get("content_flagged"):
            logger.info(
                "Moderation: comment {cid} on ticket {tid} was flagged",
                cid=comment.id,
                tid=ticket.id,
            )
