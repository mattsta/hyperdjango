# Pagination

Efficient page-based slicing for QuerySet results using COUNT + LIMIT/OFFSET. HyperDjango's paginator caches the count query, supports orphan merging, and integrates directly with ListView for template-driven pagination.

## Choosing a pagination strategy

HyperDjango gives you several safe, high-performance primitives — pick the one
that fits your data-access pattern:

| Primitive                                                      | Mechanism                                                      | Scale characteristics                                                                                                      | Use for                                                                                                                                 |
| -------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `Paginator` / `PageNumberPagination` / `LimitOffsetPagination` | `COUNT` + `LIMIT/OFFSET`                                       | Simple, jump-to-page, shows total count. But `COUNT(*)` and deep `OFFSET` both cost more as the table/offset grows.        | Bounded or moderate datasets, admin lists, "page 1..N" UIs. `page_size` is capped by `max_page_size`.                                   |
| `CursorPagination` (REST)                                      | **Keyset** on an ordered column, **HMAC-signed** cursor tokens | **O(1) regardless of depth** — no `COUNT`, no `OFFSET` scan; cursors are tamper-proof (constant-time verify, fail-closed). | Feeds/timelines and any large, deep-scrolled dataset. The scale default. Set `HYPER_SECRET_KEY` so cursors verify across cluster nodes. |
| `ServerCursorPagination` (REST)                                | PostgreSQL server-side `DECLARE CURSOR`                        | Streams a large/one-shot result set with bounded memory.                                                                   | Large exports / sequential streaming where you read once, forward-only.                                                                 |

Keyset (`CursorPagination`) is the right choice at social-media scale: offset
pagination's `COUNT(*)` and `OFFSET N` degrade on large tables, while a keyset
cursor seeks directly to the ordered position. See the REST pagination classes in
[rest.md](rest.md#pagination).

## Quick Start

```python
from hyperdjango.paginator import Paginator

paginator = Paginator(User.objects.filter(is_active=True), per_page=25)
page = await paginator.page(1)

for user in page:
    print(user.name)

print(f"Page {page.number} of {page.num_pages}")
print(f"Showing {page.start_index}-{page.end_index} of {page.count}")
```

## Paginator Class

```python
Paginator(
    queryset,  # QuerySet to paginate
    per_page=25,  # Items per page (minimum 1)
    orphans=0,  # Min items on last page before merging
    allow_empty_first_page=True,  # Allow page(1) on empty queryset
)
```

### Parameters

#### `queryset`

Any QuerySet instance. The paginator calls `.count()` once (cached) and then `.offset().limit().all()` for each page request:

```python
# Filter first, then paginate
qs = Article.objects.filter(published=True).order_by("-created_at")
paginator = Paginator(qs, per_page=10)
```

#### `per_page`

Number of items per page. Minimum enforced to 1:

```python
paginator = Paginator(queryset, per_page=50)
```

#### `orphans`

If the last page would have fewer items than `orphans`, those items are merged with the previous page. This prevents tiny final pages:

```python
# 53 items, per_page=10, orphans=0
# Result: 6 pages (last page has 3 items)
paginator = Paginator(queryset, per_page=10)

# 53 items, per_page=10, orphans=3
# Result: 5 pages (last page has 13 items)
paginator = Paginator(queryset, per_page=10, orphans=3)

# 53 items, per_page=10, orphans=5
# Result: 5 pages (last page has 13 items — 3 < 5, so merged)
paginator = Paginator(queryset, per_page=10, orphans=5)
```

The orphan calculation: `num_pages = ceil((count - orphans) / per_page)`. On the last page, all remaining items are returned (which may exceed `per_page`).

#### `allow_empty_first_page`

When `True` (default), requesting page 1 on an empty queryset returns an empty page instead of raising `EmptyPage`. When `False`, any request on an empty queryset raises `EmptyPage`:

```python
# Empty queryset, allow_empty_first_page=True (default)
paginator = Paginator(empty_qs, per_page=25)
page = await paginator.page(1)  # Returns empty page
print(len(page))  # 0

# Empty queryset, allow_empty_first_page=False
paginator = Paginator(empty_qs, per_page=25, allow_empty_first_page=False)
page = await paginator.page(1)  # Raises EmptyPage
```

### Paginator Methods

#### `await paginator.page(number)`

Return a `Page` object for the given 1-based page number. This is the primary method -- it runs the count query (cached after first call) and then fetches the slice:

```python
page = await paginator.page(1)  # First page
page = await paginator.page(3)  # Third page
```

Raises `PageNotAnInteger` if the number cannot be converted to an integer. Raises `EmptyPage` if the page number is valid but contains no results (beyond the last page, or page < 1).

#### `await paginator.get_count()`

Return the total item count. The count is cached after the first call -- subsequent calls return the cached value without hitting the database:

```python
total = await paginator.get_count()
print(f"Total items: {total}")

# Second call returns cached value (no SQL)
total_again = await paginator.get_count()
```

#### `await paginator.num_pages`

Async property returning the total number of pages:

```python
n = await paginator.num_pages
print(f"Total pages: {n}")
```

#### `await paginator.page_range`

Async property returning a `range` of valid page numbers:

```python
r = await paginator.page_range
# range(1, 6) for 5 pages
```

## Page Object

A `Page` is a dataclass representing a single page of results. It supports `len()`, `iter()`, `[]` indexing, and `bool()`.

### Properties

| Property               | Type    | Description                                          |
| ---------------------- | ------- | ---------------------------------------------------- |
| `items`                | `list`  | Model instances on this page                         |
| `number`               | `int`   | Current page number (1-based)                        |
| `num_pages`            | `int`   | Total number of pages                                |
| `count`                | `int`   | Total items across all pages                         |
| `per_page`             | `int`   | Items per page setting                               |
| `has_next`             | `bool`  | True if there is a next page                         |
| `has_previous`         | `bool`  | True if there is a previous page                     |
| `next_page_number`     | `int`   | Next page number (raises `InvalidPage` if last)      |
| `previous_page_number` | `int`   | Previous page number (raises `InvalidPage` if first) |
| `start_index`          | `int`   | 1-based index of first item on this page             |
| `end_index`            | `int`   | 1-based index of last item on this page              |
| `page_range`           | `range` | Range of all valid page numbers                      |

### Protocol Support

```python
page = await paginator.page(2)

# Length
len(page)  # Number of items on this page

# Iteration
for item in page:
    print(item.name)

# Indexing
first = page[0]
last = page[-1]
slice = page[2:5]

# Boolean (True if page has items)
if page:
    print("Has items")
```

### Navigation Properties

```python
page = await paginator.page(3)

# Check boundaries
page.has_next  # True if page 4 exists
page.has_previous  # True (page 2 exists)

# Get adjacent page numbers
page.next_page_number  # 4
page.previous_page_number  # 2

# These raise InvalidPage at boundaries
first_page = await paginator.page(1)
first_page.previous_page_number  # Raises InvalidPage

last_page = await paginator.page(page.num_pages)
last_page.next_page_number  # Raises InvalidPage
```

### Index Properties

`start_index` and `end_index` are 1-based, suitable for display ("Showing 26-50 of 100"):

```python
page = await paginator.page(2)  # per_page=25, 100 total items
page.start_index  # 26
page.end_index  # 50

# Empty page returns 0 for both
empty = await Paginator(empty_qs, per_page=25).page(1)
empty.start_index  # 0
empty.end_index  # 0
```

## Async Iteration

Iterate over all pages sequentially:

```python
paginator = Paginator(Article.objects.all(), per_page=100)

async for page in paginator:
    for article in page:
        await process(article)
    print(f"Processed page {page.number} of {page.num_pages}")
```

This is useful for batch processing all records. Each iteration fetches one page from the database.

## Error Handling

Three exception types, all inheriting from `InvalidPage`:

```python
from hyperdjango.paginator import InvalidPage, PageNotAnInteger, EmptyPage

# PageNotAnInteger — non-integer page number
try:
    page = await paginator.page("abc")
except PageNotAnInteger:
    page = await paginator.page(1)

# EmptyPage — valid integer but no items on that page
try:
    page = await paginator.page(999)
except EmptyPage:
    page = await paginator.page(1)

# EmptyPage — page number less than 1
try:
    page = await paginator.page(0)
except EmptyPage:
    page = await paginator.page(1)

# InvalidPage — catch all pagination errors
try:
    page = await paginator.page(user_input)
except InvalidPage:
    page = await paginator.page(1)
```

### Safe Page Access Pattern

A common pattern is to default to page 1 on any error:

```python
async def get_safe_page(paginator, page_number):
    """Get a page, falling back to page 1 on any error."""
    try:
        return await paginator.page(page_number)
    except InvalidPage:
        return await paginator.page(1)
```

## ListView Pagination

`ListView` integrates with Paginator automatically. Set `per_page` to enable:

```python
from hyperdjango.views import ListView


class ArticleList(ListView):
    model = Article
    per_page = 25  # Items per page (0 = no pagination)
    ordering = "-created_at"  # Default ordering
    template_name = "articles/list.html"
    context_object_name = "articles"
```

The page number is read from the `?page=` query parameter. When pagination is active, the template context includes:

| Context Variable            | Type        | Description                               |
| --------------------------- | ----------- | ----------------------------------------- |
| `page`                      | `Page`      | The current Page object                   |
| `paginator`                 | `Paginator` | The Paginator instance                    |
| `object_list` / custom name | `list`      | Items on this page (same as `page.items`) |

### How ListView Pagination Works

```python
# Internally, ListView.get() does approximately:
qs = self.get_queryset()
paginator = Paginator(qs, per_page=self.per_page)
page_num = request.GET.get("page", "1")
try:
    page = await paginator.page(page_num)
except Exception:
    page = await paginator.page(1)
items = page.items
context = self.get_context_data(page=page, paginator=paginator)
```

### Disabling Pagination

Set `per_page = 0` to disable pagination and return all results:

```python
class AllArticles(ListView):
    model = Article
    per_page = 0  # No pagination — returns all items
```

## Template Patterns

### Basic Page Navigation

```html
{# articles/list.html #} {% for article in articles %}
<div class="article">
  <h2>{{ article.title }}</h2>
  <p>{{ article.summary }}</p>
</div>
{% endfor %} {# Pagination controls #} {% if page.num_pages > 1 %}
<nav class="pagination">
  {% if page.has_previous %}
  <a href="?page=1">First</a>
  <a href="?page={{ page.previous_page_number }}">Previous</a>
  {% endif %}

  <span>Page {{ page.number }} of {{ page.num_pages }}</span>

  {% if page.has_next %}
  <a href="?page={{ page.next_page_number }}">Next</a>
  <a href="?page={{ page.num_pages }}">Last</a>
  {% endif %}
</nav>
{% endif %}
```

### Showing Item Count

```html
{% if page.count > 0 %}
<p>
  Showing {{ page.start_index }}-{{ page.end_index }} of {{ page.count }}
  results
</p>
{% else %}
<p>No results found.</p>
{% endif %}
```

### Page Number List

```html
<nav class="pagination">
  {% for p in page.page_range %} {% if p == page.number %}
  <span class="current">{{ p }}</span>
  {% else %}
  <a href="?page={{ p }}">{{ p }}</a>
  {% endif %} {% endfor %}
</nav>
```

### Preserving Other Query Parameters

When the page has search or filter parameters, preserve them across pagination links:

```html
{# If URL is /articles/?q=django&category=tutorial&page=2 #} {% if page.has_next
%}
<a
  href="?q={{ request.GET.q }}&category={{ request.GET.category }}&page={{ page.next_page_number }}"
>
  Next
</a>
{% endif %}
```

## URL Query Parameter Patterns

### Reading the Page Parameter

In a standalone route (outside ListView), read `?page=` from the request:

```python
from hyperdjango import HyperApp, Request, Response
from hyperdjango.paginator import Paginator

app = HyperApp()


@app.route("/articles")
async def article_list(request: Request) -> Response:
    page_num = request.GET.get("page", "1")

    qs = Article.objects.filter(published=True).order_by("-created_at")
    paginator = Paginator(qs, per_page=20)

    try:
        page = await paginator.page(page_num)
    except InvalidPage:
        page = await paginator.page(1)

    return Response.json(
        {
            "items": [{"id": a.id, "title": a.title} for a in page],
            "page": page.number,
            "num_pages": page.num_pages,
            "count": page.count,
            "has_next": page.has_next,
            "has_previous": page.has_previous,
        }
    )
```

### API Pagination Response Format

A standard JSON pagination envelope:

```python
@app.route("/api/users")
async def user_list(request: Request) -> Response:
    page_num = request.GET.get("page", "1")
    per_page = min(int(request.GET.get("per_page", "25")), 100)  # Cap at 100

    paginator = Paginator(User.objects.all(), per_page=per_page)

    try:
        page = await paginator.page(page_num)
    except InvalidPage:
        page = await paginator.page(1)

    return Response.json(
        {
            "results": [user.to_dict() for user in page],
            "pagination": {
                "page": page.number,
                "per_page": per_page,
                "total_pages": page.num_pages,
                "total_count": page.count,
                "has_next": page.has_next,
                "has_previous": page.has_previous,
            },
        }
    )
```

## Count Optimization

The `Paginator` caches the COUNT query result after the first call. This means:

1. The first call to `page()` runs `SELECT COUNT(*)` + `SELECT ... LIMIT/OFFSET`
2. Subsequent calls to `page()` only run the `SELECT ... LIMIT/OFFSET`
3. The count is cached for the lifetime of the Paginator instance

```python
paginator = Paginator(User.objects.all(), per_page=25)

# First page: 2 queries (COUNT + SELECT)
page1 = await paginator.page(1)

# Second page: 1 query (SELECT only — count is cached)
page2 = await paginator.page(2)

# Explicit count access (returns cached value)
total = await paginator.get_count()
```

If the underlying data changes between page requests, the cached count may be stale. For most use cases this is acceptable. For strict accuracy, create a new Paginator instance for each request.

### When COUNT is Expensive

For very large tables, `COUNT(*)` can be slow. Strategies:

**Use the query cache** to avoid repeated count queries:

```python
# Cache the queryset results (including count)
qs = Article.objects.cache(ttl=60).filter(published=True)
paginator = Paginator(qs, per_page=25)
```

**Pre-compute counts** for known queries:

```python
# Store counts in a stats table, update periodically
count = await ArticleStats.objects.get(name="published_count")
```

## Cursor-Based Pagination Pattern

For large datasets where OFFSET becomes expensive, use cursor-based pagination with a known ordering column:

```python
@app.route("/api/articles")
async def articles_cursor(request: Request) -> Response:
    cursor = request.GET.get("cursor")  # Last seen ID
    limit = min(int(request.GET.get("limit", "25")), 100)

    qs = Article.objects.filter(published=True).order_by("-id")

    if cursor:
        # Fetch items after the cursor
        qs = qs.filter(id__lt=int(cursor))

    items = await qs.limit(limit + 1).all()  # Fetch one extra to detect next page

    has_next = len(items) > limit
    if has_next:
        items = items[:limit]

    next_cursor = str(items[-1].id) if items and has_next else None

    return Response.json(
        {
            "results": [a.to_dict() for a in items],
            "next_cursor": next_cursor,
            "has_next": has_next,
        }
    )
```

Cursor-based pagination advantages:

- O(1) performance regardless of page depth (no OFFSET scan)
- Stable results even when data is inserted/deleted between pages
- Works well with real-time feeds and infinite scroll UIs

Cursor-based pagination limitations:

- Cannot jump to arbitrary page numbers
- Requires a unique, ordered column (typically `id` or a timestamp)
- Only supports forward/backward navigation, not random access

## Practical Examples

### Blog with Pagination

```python
class BlogList(ListView):
    model = Post
    per_page = 10
    ordering = "-published_at"
    template_name = "blog/list.html"
    context_object_name = "posts"

    def get_queryset(self):
        qs = Post.objects.filter(published=True)
        category = self.request.GET.get("category")
        if category:
            qs = qs.filter(category=category)
        return qs.order_by("-published_at")
```

### Admin-Style Full Pagination

```python
@app.route("/admin/users")
async def admin_users(request: Request) -> Response:
    search = request.GET.get("q", "")
    page_num = request.GET.get("page", "1")

    qs = User.objects.order_by("name")
    if search:
        qs = qs.filter(name__icontains=search)

    paginator = Paginator(qs, per_page=50, orphans=5)

    try:
        page = await paginator.page(page_num)
    except InvalidPage:
        page = await paginator.page(1)

    return render(
        request,
        "admin/users.html",
        {
            "page": page,
            "search": search,
        },
    )
```

### Batch Processing All Records

```python
async def reindex_all_articles():
    """Process all articles in batches of 200."""
    paginator = Paginator(Article.objects.all(), per_page=200)

    async for page in paginator:
        for article in page:
            await search_index.update(article)
        print(f"Reindexed page {page.number}/{page.num_pages}")
```

## Reference

### Imports

```python
from hyperdjango.paginator import (
    Paginator,  # Main paginator class
    Page,  # Page dataclass
    InvalidPage,  # Base exception
    PageNotAnInteger,  # Non-integer page number
    EmptyPage,  # Page out of range
)
```

### Paginator API Summary

| Method / Property             | Returns | Description                 |
| ----------------------------- | ------- | --------------------------- |
| `await page(number)`          | `Page`  | Get page by 1-based number  |
| `await get_count()`           | `int`   | Total items (cached)        |
| `await num_pages`             | `int`   | Total page count            |
| `await page_range`            | `range` | Range of valid page numbers |
| `async for page in paginator` | `Page`  | Iterate all pages           |

### Page API Summary

| Property               | Returns  | Description                     |
| ---------------------- | -------- | ------------------------------- |
| `items`                | `list`   | Items on this page              |
| `number`               | `int`    | Current page number (1-based)   |
| `num_pages`            | `int`    | Total pages                     |
| `count`                | `int`    | Total items                     |
| `per_page`             | `int`    | Items per page setting          |
| `has_next`             | `bool`   | Next page exists                |
| `has_previous`         | `bool`   | Previous page exists            |
| `next_page_number`     | `int`    | Next page (raises if last)      |
| `previous_page_number` | `int`    | Previous page (raises if first) |
| `start_index`          | `int`    | 1-based first item index        |
| `end_index`            | `int`    | 1-based last item index         |
| `page_range`           | `range`  | All valid page numbers          |
| `len(page)`            | `int`    | Item count on this page         |
| `iter(page)`           | iterator | Iterate items                   |
| `page[i]`              | item     | Index into items                |
| `bool(page)`           | `bool`   | True if page has items          |
