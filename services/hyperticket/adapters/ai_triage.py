"""
AI Triage Adapter — auto-classify tickets on creation.

Demo adapter that simulates AI classification. In production, this would
call an LLM API to analyze ticket title + description and suggest:
  - Priority adjustment
  - Team routing
  - Tag suggestions

Demonstrates the adapter pipeline's on_create_post hook.
"""

from hyperdjango.logging import logger

from ..models import Tag, Ticket, TicketTag
from .protocols import AdapterContext


class AITriageAdapter:
    """Auto-classify tickets using keyword matching (demo; production uses LLM)."""

    # Keyword → tag mapping for demo classification
    _KEYWORD_TAGS: dict[str, str] = {
        "login": "login",
        "password": "login",
        "billing": "billing",
        "invoice": "billing",
        "payment": "billing",
        "slow": "performance",
        "crash": "performance",
        "timeout": "performance",
        "api": "api",
        "endpoint": "api",
        "security": "security",
        "vulnerability": "security",
    }

    async def on_create_pre(self, ctx: AdapterContext, data: dict) -> dict:
        """Pre-hook: no modifications in triage (classification happens post-create)."""
        return data

    async def on_create_post(self, ctx: AdapterContext, ticket: Ticket) -> None:
        """Post-hook: classify ticket by keywords, auto-tag."""
        title_lower = (ticket.title or "").lower()
        desc_lower = (ticket.description or "").lower()
        combined = f"{title_lower} {desc_lower}"

        matched_tags: set[str] = set()
        for keyword, tag_name in self._KEYWORD_TAGS.items():
            if keyword in combined:
                matched_tags.add(tag_name)

        if not matched_tags:
            return

        # Find matching Tag records and apply
        for tag_name in matched_tags:
            tag = await Tag.objects.filter(name=tag_name).first()
            if tag:
                existing = await TicketTag.objects.filter(
                    ticket_id=ticket.id, tag_id=tag.id
                ).first()
                if not existing:
                    await TicketTag(
                        tenant_id=ctx.tenant_id,
                        ticket_id=ticket.id,
                        tag_id=tag.id,
                    ).save()

        logger.info(
            "AI triage: ticket {tid} auto-tagged with {tags}",
            tid=ticket.id,
            tags=matched_tags,
        )

    async def on_update_pre(self, ctx, ticket, changes):
        return changes

    async def on_update_post(self, ctx, ticket, changes):
        pass

    async def on_status_change(self, ctx, ticket, old_status_id, new_status_id):
        pass

    async def on_assign(self, ctx, ticket, assignee_id):
        pass

    async def on_close(self, ctx, ticket):
        pass

    async def on_merge(self, ctx, source, target):
        pass
