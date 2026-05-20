"""
Paginator — page-based slicing for QuerySet results.

Efficiently paginates database queries using LIMIT/OFFSET (count query + data query).

Usage:
    from hyperdjango.paginator import Paginator

    paginator = Paginator(User.objects.filter(is_active=True), per_page=25)
    page = await paginator.page(1)

    page.items          # list of model instances
    page.number         # current page number (1-based)
    page.has_next       # True if there's a next page
    page.has_previous   # True if there's a previous page
    page.num_pages      # total number of pages
    page.count          # total number of items across all pages
    page.start_index    # 1-based index of first item on this page
    page.end_index      # 1-based index of last item on this page
    page.page_range     # range(1, num_pages + 1)

    # Iterate pages
    async for page in paginator:
        for item in page.items:
            process(item)
"""

import math
import threading
import time
from dataclasses import dataclass

from hyperdjango.conf import DEFAULT_PAGE_SIZE

# Process-wide COUNT(*) cache, keyed by the compiled count query signature.
# COUNT(*) is the expensive half of pagination and is identical across every
# page of the same query; a per-Paginator instance (one per request) never
# gets to reuse it. This lets separate Paginator instances for the same query
# share a recent count under a short, opt-in TTL. Entries store (expiry, count).
_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_COUNT_CACHE_LOCK = threading.Lock()


def _prune_count_cache(now: float) -> None:
    """Drop expired entries (called opportunistically under the lock)."""
    expired = [k for k, (exp, _) in _COUNT_CACHE.items() if exp <= now]
    for k in expired:
        del _COUNT_CACHE[k]


def clear_count_cache() -> None:
    """Clear the shared pagination COUNT cache (useful in tests)."""
    with _COUNT_CACHE_LOCK:
        _COUNT_CACHE.clear()


class InvalidPage(Exception):
    """Raised when a page number is invalid."""


class PageNotAnInteger(InvalidPage):
    """Raised when a page number is not an integer."""


class EmptyPage(InvalidPage):
    """Raised when a page number is valid but no items exist on that page."""


@dataclass(slots=True)
class Page:
    """A single page of paginated results."""

    items: list
    number: int
    num_pages: int
    count: int
    per_page: int

    @property
    def has_next(self) -> bool:
        return self.number < self.num_pages

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def next_page_number(self) -> int:
        if not self.has_next:
            raise InvalidPage(f"Page {self.number} is the last page")
        return self.number + 1

    @property
    def previous_page_number(self) -> int:
        if not self.has_previous:
            raise InvalidPage(f"Page {self.number} is the first page")
        return self.number - 1

    @property
    def start_index(self) -> int:
        """1-based index of the first item on this page."""
        if not self.items:
            return 0
        return (self.number - 1) * self.per_page + 1

    @property
    def end_index(self) -> int:
        """1-based index of the last item on this page."""
        if not self.items:
            return 0
        return self.start_index + len(self.items) - 1

    @property
    def page_range(self) -> range:
        """Range of all valid page numbers."""
        return range(1, self.num_pages + 1)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def __bool__(self) -> bool:
        return len(self.items) > 0


class Paginator:
    """Paginates a QuerySet using efficient COUNT + LIMIT/OFFSET queries.

    Args:
        queryset: A QuerySet instance to paginate
        per_page: Number of items per page (default: 25)
        orphans: Minimum items on last page before merging with previous (default: 0)
        allow_empty_first_page: If True, page(1) returns empty page instead of raising
        count: A known total count to reuse (skips the COUNT query entirely).
            Use when the caller already knows the total for this exact query.
        count_ttl: If > 0, cache/reuse the COUNT(*) result across Paginator
            instances for the same query for this many seconds. 0 (default)
            keeps the old per-instance-only behavior — no cross-request reuse.
    """

    def __init__(
        self,
        queryset,
        per_page: int = DEFAULT_PAGE_SIZE,
        orphans: int = 0,
        allow_empty_first_page: bool = True,
        count: int | None = None,
        count_ttl: float = 0.0,
    ):
        self.queryset = queryset
        self.per_page = max(1, per_page)
        self.orphans = orphans
        self.allow_empty_first_page = allow_empty_first_page
        self._count: int | None = count
        self._num_pages: int | None = None
        self.count_ttl = count_ttl

    async def page(self, number) -> Page:
        """Return a Page object for the given 1-based page number."""
        number = self._validate_number(number)
        count = await self.get_count()
        num_pages = self._compute_num_pages(count)

        if number > num_pages:
            if number == 1 and self.allow_empty_first_page:
                return Page(
                    items=[],
                    number=1,
                    num_pages=num_pages,
                    count=count,
                    per_page=self.per_page,
                )
            raise EmptyPage(f"Page {number} contains no results (total: {count})")

        offset = (number - 1) * self.per_page

        # On the last page, fetch remaining items (may include orphans)
        limit = count - offset if number == num_pages else self.per_page

        # Skip the round trip when the page is provably empty (e.g. an empty
        # result set, where the last page computes limit=0). `LIMIT 0` returns
        # nothing but still costs a query.
        if limit <= 0:
            items = []
        else:
            items = await self.queryset.offset(offset).limit(limit).all()

        return Page(
            items=items,
            number=number,
            num_pages=num_pages,
            count=count,
            per_page=self.per_page,
        )

    def _count_signature(self) -> str | None:
        """Stable key for the COUNT query, or None if it can't be derived."""
        try:
            sql, params = self.queryset._build_count()
            return f"{sql}|{tuple(params)!r}"
        # blind-except: key only enables cross-request COUNT sharing; if it can't be derived return None to disable caching — the real COUNT still runs and would surface any genuine query error there.
        except Exception:
            return None

    async def get_count(self) -> int:
        """Return total number of items.

        Cached on the instance after the first call. When ``count_ttl > 0`` the
        result is additionally shared, keyed by the compiled count query, so
        sibling Paginators (e.g. successive requests paging the same query)
        don't each re-run COUNT(*).
        """
        if self._count is not None:
            return self._count

        if self.count_ttl > 0:
            sig = self._count_signature()
            if sig is not None:
                now = time.monotonic()
                with _COUNT_CACHE_LOCK:
                    hit = _COUNT_CACHE.get(sig)
                    if hit is not None and hit[0] > now:
                        self._count = hit[1]
                        return self._count
                # Miss — compute outside the lock, then store.
                value = await self.queryset.count()
                with _COUNT_CACHE_LOCK:
                    _prune_count_cache(now)
                    _COUNT_CACHE[sig] = (now + self.count_ttl, value)
                self._count = value
                return self._count

        self._count = await self.queryset.count()
        return self._count

    @property
    async def num_pages(self) -> int:
        """Total number of pages."""
        count = await self.get_count()
        return self._compute_num_pages(count)

    @property
    async def page_range(self) -> range:
        """Range of valid page numbers."""
        n = await self.num_pages
        return range(1, n + 1)

    def _validate_number(self, number) -> int:
        """Validate and return the page number as an integer."""
        try:
            if isinstance(number, float) and not number.is_integer():
                raise ValueError("Page number must be an integer")
            number = int(number)
        except TypeError, ValueError:
            raise PageNotAnInteger(f"Page number is not an integer: {number!r}")
        if number < 1:
            raise EmptyPage(f"Page number must be >= 1, got {number}")
        return number

    def _compute_num_pages(self, count: int) -> int:
        """Compute total pages, accounting for orphans."""
        if count == 0 and not self.allow_empty_first_page:
            return 0
        hits = max(1, count - self.orphans)
        return math.ceil(hits / self.per_page)

    async def __aiter__(self):
        """Async iterate over all pages."""
        count = await self.get_count()
        num_pages = self._compute_num_pages(count)
        for page_num in range(1, num_pages + 1):
            yield await self.page(page_num)


# An invalid page (out of range / not an integer) is a 404, not a 500.
from hyperdjango.exceptions import register_exception_status as _register_exc_status

_register_exc_status(InvalidPage, 404)
