"""
Adapter Registry — global and per-tenant adapter management.

Thread-safe registry for pluggable adapters at every entry point.
Adapters are called in registration order. Errors in one adapter
don't prevent others from running (error isolation).
"""

import threading
from typing import Any

from hyperdjango.logging import logger

from .protocols import (
    AdapterContext,
    AssignmentAdapter,
    CommentAdapter,
    ExportAdapter,
    SearchAdapter,
    TicketAdapter,
    WorkflowActionAdapter,
)


class AdapterRegistry:
    """Global registry of adapters. Thread-safe. Supports per-tenant overrides."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Global adapters (tenant_id=None) + per-tenant (tenant_id=int)
        self._ticket: dict[int | None, list[TicketAdapter]] = {}
        self._comment: dict[int | None, list[CommentAdapter]] = {}
        self._assignment: dict[int | None, list[AssignmentAdapter]] = {}
        self._search: dict[int | None, list[SearchAdapter]] = {}
        self._export: dict[int | None, list[ExportAdapter]] = {}
        self._workflow_actions: dict[str, WorkflowActionAdapter] = {}

    def register_ticket_adapter(
        self, adapter: TicketAdapter, tenant_id: int | None = None
    ) -> None:
        with self._lock:
            self._ticket.setdefault(tenant_id, []).append(adapter)

    def register_comment_adapter(
        self, adapter: CommentAdapter, tenant_id: int | None = None
    ) -> None:
        with self._lock:
            self._comment.setdefault(tenant_id, []).append(adapter)

    def register_assignment_adapter(
        self, adapter: AssignmentAdapter, tenant_id: int | None = None
    ) -> None:
        with self._lock:
            self._assignment.setdefault(tenant_id, []).append(adapter)

    def register_search_adapter(
        self, adapter: SearchAdapter, tenant_id: int | None = None
    ) -> None:
        with self._lock:
            self._search.setdefault(tenant_id, []).append(adapter)

    def register_export_adapter(
        self, adapter: ExportAdapter, tenant_id: int | None = None
    ) -> None:
        with self._lock:
            self._export.setdefault(tenant_id, []).append(adapter)

    def register_workflow_action(self, adapter: WorkflowActionAdapter) -> None:
        with self._lock:
            self._workflow_actions[adapter.action_name] = adapter

    def _get_adapters(
        self, registry: dict[int | None, list[Any]], tenant_id: int
    ) -> list[Any]:
        """Get global + tenant-specific adapters in registration order."""
        result = list(registry.get(None, []))
        result.extend(registry.get(tenant_id, []))
        return result

    # ── Ticket hooks ──────────────────────────────────────────

    async def run_ticket_create_pre(
        self, ctx: AdapterContext, data: dict[str, Any]
    ) -> dict[str, Any]:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                data = await adapter.on_create_pre(ctx, data)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_create_pre error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )
        return data

    async def run_ticket_create_post(self, ctx: AdapterContext, ticket: Any) -> None:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                await adapter.on_create_post(ctx, ticket)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_create_post error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )

    async def run_ticket_update_pre(
        self, ctx: AdapterContext, ticket: Any, changes: dict[str, Any]
    ) -> dict[str, Any]:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                changes = await adapter.on_update_pre(ctx, ticket, changes)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_update_pre error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )
        return changes

    async def run_ticket_update_post(
        self, ctx: AdapterContext, ticket: Any, changes: dict[str, Any]
    ) -> None:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                await adapter.on_update_post(ctx, ticket, changes)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_update_post error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )

    async def run_ticket_close(self, ctx: AdapterContext, ticket: Any) -> None:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                await adapter.on_close(ctx, ticket)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_close error: {e}", a=type(adapter).__name__, e=exc
                )

    async def run_ticket_merge(
        self, ctx: AdapterContext, source: Any, target: Any
    ) -> None:
        for adapter in self._get_adapters(self._ticket, ctx.tenant_id):
            try:
                await adapter.on_merge(ctx, source, target)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_merge error: {e}", a=type(adapter).__name__, e=exc
                )

    # ── Comment hooks ─────────────────────────────────────────

    async def run_comment_pre(
        self, ctx: AdapterContext, ticket: Any, data: dict[str, Any]
    ) -> dict[str, Any]:
        for adapter in self._get_adapters(self._comment, ctx.tenant_id):
            try:
                data = await adapter.on_comment_pre(ctx, ticket, data)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_comment_pre error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )
        return data

    async def run_comment_post(
        self, ctx: AdapterContext, ticket: Any, comment: Any
    ) -> None:
        for adapter in self._get_adapters(self._comment, ctx.tenant_id):
            try:
                await adapter.on_comment_post(ctx, ticket, comment)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_comment_post error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )

    # ── Search hooks ──────────────────────────────────────────

    async def run_search_pre(self, ctx: AdapterContext, query: str) -> str:
        for adapter in self._get_adapters(self._search, ctx.tenant_id):
            try:
                query = await adapter.on_search_pre(ctx, query)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_search_pre error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )
        return query

    async def run_search_post(
        self, ctx: AdapterContext, query: str, results: list[Any]
    ) -> list[Any]:
        for adapter in self._get_adapters(self._search, ctx.tenant_id):
            try:
                results = await adapter.on_search_post(ctx, query, results)
            except Exception as exc:
                logger.error(
                    "Adapter {a} on_search_post error: {e}",
                    a=type(adapter).__name__,
                    e=exc,
                )
        return results

    # ── Workflow action execution ─────────────────────────────

    async def execute_workflow_action(
        self, action_name: str, ctx: AdapterContext, ticket: Any, params: dict[str, Any]
    ) -> bool:
        adapter = self._workflow_actions.get(action_name)
        if adapter is None:
            return False
        try:
            await adapter.execute(ctx, ticket, params)
            return True
        except Exception as exc:
            logger.error("Workflow action {a} error: {e}", a=action_name, e=exc)
            return False


# Module-level singleton
adapter_registry = AdapterRegistry()
