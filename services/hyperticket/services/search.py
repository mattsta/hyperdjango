"""
Full-text search service — FTS Expression classes + trigram similarity.

Uses SearchVector/SearchQuery/SearchRank from postgres.py for ranked full-text
search, and TrigramSimilarity for fuzzy fallback on ticket titles.
"""

from hyperdjango.database import get_db
from hyperdjango.postgres import (
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramSimilarity,
)

from ..models import Ticket


async def search_tickets(
    query: str,
    tenant_id: int,
    limit: int = 25,
) -> list[dict[str, object]]:
    """Search tickets by full-text + trigram similarity.

    Uses ORM FTS Expression classes for ranked search:
    - SearchRank for full-text relevance scoring
    - TrigramSimilarity for fuzzy title matching

    The WHERE clause uses OR (FTS match OR trigram > 0.15) via where_raw,
    since the ORM doesn't support OR across different expression types natively.
    """
    if not query or not query.strip():
        return []

    clean_query = query.strip()

    # Build FTS expressions
    vector = SearchVector(["title", "description"], config="english")
    sq = SearchQuery(clean_query, config="english", search_type="plain")
    rank = SearchRank(vector, sq)
    sim = TrigramSimilarity("title", clean_query)

    # Annotate with both rank and similarity
    qs = Ticket.objects.filter(
        tenant_id=tenant_id, is_deleted=False, is_current=True
    ).annotate(rank=rank, sim=sim)

    # WHERE: FTS match OR trigram similarity > 0.15
    # Uses where_raw for the OR condition across two expression types
    vector_sql, _ = vector.as_sql()
    where_sql = (
        f"(({vector_sql}) @@ plainto_tsquery('english', {{idx}}) "
        f'OR similarity("title", {{idx}}) > 0.15)'
    )
    qs = qs.where_raw(where_sql, clean_query, clean_query)

    tickets = await qs.order_by("-rank").limit(limit).all()

    results: list[dict[str, object]] = []
    for t in tickets:
        desc = t.description or ""
        snippet = desc[:200] + "..." if len(desc) > 200 else desc
        results.append(
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "title": t.title,
                "snippet": snippet,
                "rank": float(t.rank) if t.rank else 0.0,
                "similarity": float(t.sim) if t.sim else 0.0,
            }
        )

    return results


async def search_comments(
    query: str,
    tenant_id: int,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Search comments by full-text across all tickets in tenant.

    Uses JOIN with tickets table for ticket_number — kept as raw SQL
    since cross-table JOINs with FTS require select_related + where_raw
    which is less clear than the direct query for this use case.
    """
    if not query or not query.strip():
        return []

    db = get_db()
    rows = await db.query(
        "SELECT c.id, c.ticket_id, c.body, c.author_type, c.created_at, "
        "  t.ticket_number "
        "FROM ht_comments c "
        "JOIN ht_tickets t ON c.ticket_id = t.id "
        "WHERE c.tenant_id = $1 "
        "  AND c.is_deleted = FALSE "
        "  AND to_tsvector('english', c.body) @@ plainto_tsquery('english', $2) "
        "ORDER BY c.created_at DESC "
        "LIMIT $3",
        tenant_id,
        query.strip(),
        limit,
    )

    return [
        {
            "id": row["id"],
            "ticket_id": row["ticket_id"],
            "ticket_number": row["ticket_number"],
            "body_snippet": (row["body"] or "")[:200],
            "author_type": row["author_type"],
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
