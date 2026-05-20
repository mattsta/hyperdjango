"""
Adapter protocols — pluggable extension points for every major data flow.

Each protocol defines pre/post hooks that adapters implement.
Pre hooks can modify input data, reject operations, or enrich context.
Post hooks can observe results, trigger side effects, or enrich output.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hyperdjango import Request


@dataclass
class AdapterContext:
    """Passed to every adapter. Mutable — adapters annotate for downstream use."""

    tenant_id: int
    actor_type: str  # "agent", "customer", "system", "api"
    actor_id: int
    request: Request | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TicketAdapter(Protocol):
    """Protocol for ticket lifecycle adapters."""

    async def on_create_pre(
        self, ctx: AdapterContext, data: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def on_create_post(self, ctx: AdapterContext, ticket: Any) -> None: ...
    async def on_update_pre(
        self, ctx: AdapterContext, ticket: Any, changes: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def on_update_post(
        self, ctx: AdapterContext, ticket: Any, changes: dict[str, Any]
    ) -> None: ...
    async def on_status_change(
        self, ctx: AdapterContext, ticket: Any, old_status_id: int, new_status_id: int
    ) -> None: ...
    async def on_assign(
        self, ctx: AdapterContext, ticket: Any, assignee_id: int
    ) -> None: ...
    async def on_close(self, ctx: AdapterContext, ticket: Any) -> None: ...
    async def on_merge(self, ctx: AdapterContext, source: Any, target: Any) -> None: ...


@runtime_checkable
class CommentAdapter(Protocol):
    """Protocol for comment lifecycle adapters."""

    async def on_comment_pre(
        self, ctx: AdapterContext, ticket: Any, data: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def on_comment_post(
        self, ctx: AdapterContext, ticket: Any, comment: Any
    ) -> None: ...


@runtime_checkable
class AssignmentAdapter(Protocol):
    """Protocol for assignment decision adapters."""

    async def on_assignment_pre(
        self, ctx: AdapterContext, ticket: Any, candidates: list[Any]
    ) -> list[Any]: ...
    async def on_assignment_post(
        self, ctx: AdapterContext, ticket: Any, assigned_agent: Any
    ) -> None: ...


@runtime_checkable
class SearchAdapter(Protocol):
    """Protocol for search enrichment adapters."""

    async def on_search_pre(self, ctx: AdapterContext, query: str) -> str: ...
    async def on_search_post(
        self, ctx: AdapterContext, query: str, results: list[Any]
    ) -> list[Any]: ...


@runtime_checkable
class ExportAdapter(Protocol):
    """Protocol for export transformation adapters."""

    async def on_export_row(
        self, ctx: AdapterContext, row: dict[str, Any]
    ) -> dict[str, Any]: ...


@runtime_checkable
class WorkflowActionAdapter(Protocol):
    """Protocol for custom workflow actions. Registered by action name."""

    action_name: str

    async def execute(
        self, ctx: AdapterContext, ticket: Any, params: dict[str, Any]
    ) -> None: ...
