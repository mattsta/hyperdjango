# Expressions & Aggregation

F expressions, Value expressions, aggregate functions, conditional expressions (Case/When), and advanced query patterns for database-level computations. All expressions generate parameterized PostgreSQL SQL -- no raw string interpolation.

## F Expressions

`F()` references a database column by name, enabling database-level operations without loading values into Python. This avoids race conditions and round-trips.

```python
from hyperdjango.expressions import F
```

### Atomic Updates

Update a column using its current database value -- no SELECT needed, no race condition:

```python
# Increment views atomically
await Article.objects.filter(id=1).update(views=F("views") + 1)
# SQL: UPDATE articles SET views = views + 1 WHERE id = $1

# Decrement stock
await Product.objects.filter(id=42).update(stock=F("stock") - 1)
# SQL: UPDATE products SET stock = stock - 1 WHERE id = $1

# Percentage increase
await Product.objects.filter(category="electronics").update(price=F("price") * 1.1)
# SQL: UPDATE products SET price = price * 1.1 WHERE category = $1
```

### Column Comparison in Filters

Compare two columns in the same row:

```python
# Articles where views exceed the minimum threshold
articles = await Article.objects.filter(views__gt=F("min_views")).all()
# SQL: SELECT * FROM articles WHERE views > min_views

# Orders shipped after creation
late_orders = await Order.objects.filter(shipped_at__gt=F("created_at")).all()
# SQL: SELECT * FROM orders WHERE shipped_at > created_at

# Products with stock below reorder level
low_stock = await Product.objects.filter(stock__lt=F("reorder_level")).all()
# SQL: SELECT * FROM products WHERE stock < reorder_level
```

### Arithmetic with F

F expressions support all arithmetic operators: `+`, `-`, `*`, `/`, `%`:

```python
# Compute profit margin
products = await Product.objects.annotate(
    profit=F("price") - F("cost"),
).all()

# Revenue per unit
products = await Product.objects.annotate(
    revenue=F("price") * F("quantity_sold"),
).all()

# Percentage markup
products = await Product.objects.annotate(
    markup_pct=(F("price") - F("cost")) / F("cost") * 100,
).all()

# Modulo
items = await Item.objects.annotate(
    remainder=F("quantity") % 12,
).all()
```

### Negation

Negate an expression with the unary `-` operator:

```python
# Negative balance
accounts = (
    await Account.objects.annotate(
        debt=-F("balance"),
    )
    .filter(debt__gt=0)
    .all()
)
```

### F with Annotations

Compute derived columns and use them in subsequent filters or ordering:

```python
from hyperdjango.expressions import F, Value

# Popularity score combining views and likes
articles = (
    await Article.objects.annotate(
        popularity=F("views") + F("likes") * 2,
    )
    .order_by("-popularity")
    .all()
)

# Discount amount
products = (
    await Product.objects.annotate(
        discount_amount=F("price") * F("discount_rate"),
        final_price=F("price") - F("price") * F("discount_rate"),
    )
    .filter(final_price__lt=50)
    .all()
)
```

### Ordering with F

F expressions work in `order_by()` for column-based sorting:

```python
# Order by computed expression
articles = (
    await Article.objects.annotate(
        score=F("views") + F("likes") * 3,
    )
    .order_by("-score")
    .all()
)
```

### nulls_first / nulls_last

For ordering with NULL handling, use raw SQL ordering:

```python
# NULL values last
articles = await Article.objects.order_by("-published_at").all()
# PostgreSQL default: NULLs first for DESC, NULLs last for ASC
```

## Value Expressions

`Value()` wraps a literal Python value for use in SQL expressions:

```python
from hyperdjango.expressions import Value

# Annotate with a constant
articles = await Article.objects.annotate(
    source=Value("database"),
).all()

# NULL value
articles = await Article.objects.annotate(
    notes=Value(None),
).all()
# SQL: SELECT *, NULL AS notes FROM articles
```

### Value with Arithmetic

Mix Value with F for computed expressions:

```python
# Fixed tax rate
orders = await Order.objects.annotate(
    tax=F("subtotal") * Value(0.08),
    total=F("subtotal") * Value(1.08),
).all()
```

## Aggregate Functions

Compute summary statistics across an entire QuerySet. Aggregates return a dictionary of results.

```python
from hyperdjango.expressions import Count, Sum, Avg, Max, Min, StdDev, Variance
```

### Single Aggregation

```python
result = await Article.objects.aggregate(total=Count("id"))
# {"total": 150}

result = await Order.objects.aggregate(revenue=Sum("amount"))
# {"revenue": Decimal("25000.00")}
```

### Multiple Aggregations

```python
stats = await Order.objects.aggregate(
    count=Count("id"),
    total_revenue=Sum("amount"),
    avg_order=Avg("amount"),
    largest_order=Max("amount"),
    smallest_order=Min("amount"),
)
# {"count": 500, "total_revenue": 25000.0, "avg_order": 50.0, ...}
```

### Available Aggregate Functions

| Function                        | SQL                     | Description              |
| ------------------------------- | ----------------------- | ------------------------ |
| `Count("field")`                | `COUNT(field)`          | Count non-null values    |
| `Count("field", distinct=True)` | `COUNT(DISTINCT field)` | Count unique values      |
| `Count("*")`                    | `COUNT(*)`              | Count all rows           |
| `Sum("field")`                  | `SUM(field)`            | Sum of values            |
| `Sum("field", distinct=True)`   | `SUM(DISTINCT field)`   | Sum of unique values     |
| `Avg("field")`                  | `AVG(field)`            | Average                  |
| `Avg("field", distinct=True)`   | `AVG(DISTINCT field)`   | Average of unique values |
| `Max("field")`                  | `MAX(field)`            | Maximum value            |
| `Min("field")`                  | `MIN(field)`            | Minimum value            |
| `StdDev("field")`               | `STDDEV(field)`         | Standard deviation       |
| `Variance("field")`             | `VARIANCE(field)`       | Variance                 |

### Count with distinct

Count unique values to avoid double-counting in JOINs:

```python
# Count unique authors who have published articles
result = await Article.objects.aggregate(
    unique_authors=Count("author_id", distinct=True),
)
# {"unique_authors": 42}

# Total articles vs unique categories
stats = await Article.objects.aggregate(
    total=Count("id"),
    categories=Count("category_id", distinct=True),
)
```

### Aggregate with filter_expr

Filter which rows are included in an aggregate using `filter_expr`:

```python
# Count by status using conditional aggregation
stats = await Order.objects.aggregate(
    total=Count("id"),
    paid=Count("id", filter_expr={"status": "paid"}),
    pending=Count("id", filter_expr={"status": "pending"}),
    refunded=Count("id", filter_expr={"status": "refunded"}),
)
# SQL: COUNT(id) FILTER (WHERE status = 'paid'), etc.

# Sum only completed orders
revenue = await Order.objects.aggregate(
    total_revenue=Sum("amount", filter_expr={"status": "completed"}),
)
```

## Annotations

Add computed columns to each row in the result set. Annotations are available as attributes on the returned model instances.

### Count Related Objects

```python
# Count articles per author
authors = (
    await Author.objects.annotate(
        article_count=Count("articles"),
    )
    .order_by("-article_count")
    .all()
)

for author in authors:
    print(f"{author.name}: {author.article_count} articles")
```

### Sum Related Values

```python
# Total views per author
authors = await Author.objects.annotate(
    total_views=Sum("articles__views"),
).all()

for author in authors:
    print(f"{author.name}: {author.total_views} total views")
```

### Filter After Annotation

Annotated fields can be used in subsequent `.filter()` calls:

```python
# Authors with more than 10 articles
prolific = (
    await Author.objects.annotate(
        article_count=Count("articles"),
    )
    .filter(article_count__gt=10)
    .all()
)
```

### Multiple Annotations

```python
authors = (
    await Author.objects.annotate(
        article_count=Count("articles"),
        total_views=Sum("articles__views"),
        avg_views=Avg("articles__views"),
        best_article=Max("articles__views"),
    )
    .order_by("-total_views")
    .all()
)
```

## Conditional Expressions

### Case/When

SQL CASE statements in Python. Evaluate conditions in order and return the first matching result:

```python
from hyperdjango.expressions import Case, When, Value

# Categorize articles by view count
articles = await Article.objects.annotate(
    popularity=Case(
        When(views__gt=10000, then=Value("viral")),
        When(views__gt=1000, then=Value("popular")),
        When(views__gt=100, then=Value("growing")),
        default=Value("new"),
    ),
).all()

for article in articles:
    print(f"{article.title}: {article.popularity}")
```

### When Conditions

`When` supports all the same lookups as `filter()`:

```python
# Exact match
When(status="active", then=Value(1))

# Comparison operators
When(views__gt=1000, then=Value("popular"))
When(price__lte=10, then=Value("budget"))

# String lookups
When(name__icontains="sale", then=Value(True))
When(email__endswith="@company.com", then=Value("internal"))

# NULL checks
When(deleted_at__isnull=True, then=Value("active"))
When(deleted_at__isnull=False, then=Value("deleted"))

# Range
When(age__range=(18, 65), then=Value("working_age"))

# Multiple conditions (AND)
When(status="active", verified=True, then=Value("trusted"))
```

### Conditional Update

Use Case/When in `.update()` to conditionally set values:

```python
# Tier-based discount
await Product.objects.update(
    discount=Case(
        When(price__gt=100, then=Value(0.15)),
        When(price__gt=50, then=Value(0.10)),
        When(price__gt=20, then=Value(0.05)),
        default=Value(0),
    ),
)
```

### Conditional Aggregation

Count or sum based on conditions:

```python
# Count orders by status
stats = await Order.objects.aggregate(
    paid=Count(Case(When(status="paid", then=1))),
    pending=Count(Case(When(status="pending", then=1))),
    cancelled=Count(Case(When(status="cancelled", then=1))),
)
# {"paid": 350, "pending": 100, "cancelled": 50}

# Sum revenue by category
revenue = await Order.objects.aggregate(
    electronics=Sum(Case(When(category="electronics", then=F("amount")))),
    clothing=Sum(Case(When(category="clothing", then=F("amount")))),
    books=Sum(Case(When(category="books", then=F("amount")))),
)
```

### Conditional Annotation

Annotate each row with a computed conditional value:

```python
# User risk level based on multiple factors
users = (
    await User.objects.annotate(
        risk_level=Case(
            When(login_failures__gt=10, then=Value("high")),
            When(login_failures__gt=3, then=Value("medium")),
            default=Value("low"),
        ),
    )
    .filter(risk_level="high")
    .all()
)
```

## Coalesce

Return the first non-NULL value from a list of expressions:

```python
from hyperdjango.expressions import Coalesce, F, Value

# Use display_name if set, otherwise fall back to username
users = await User.objects.annotate(
    name=Coalesce(F("display_name"), F("username"), Value("Anonymous")),
).all()
# SQL: COALESCE(display_name, username, 'Anonymous')

# Default zero for nullable numeric fields
products = await Product.objects.annotate(
    effective_discount=Coalesce(F("discount"), Value(0)),
).all()
```

## Cast

Type-cast an expression to a different SQL type:

```python
from hyperdjango.expressions import Cast, F

# Cast string to integer
items = await Item.objects.annotate(
    numeric_code=Cast(F("code"), "INTEGER"),
).all()
# SQL: CAST(code AS INTEGER)

# Cast to text
items = await Item.objects.annotate(
    id_text=Cast(F("id"), "TEXT"),
).all()
```

## Subquery

Use a QuerySet as a subquery expression within annotations or filters:

```python
from hyperdjango.expressions import Subquery

# Latest order date per customer
latest_order = (
    Order.objects.filter(
        customer_id=F("id"),
    )
    .order_by("-created_at")
    .values("created_at")
    .limit(1)
)

customers = await Customer.objects.annotate(
    last_order_date=Subquery(latest_order),
).all()
```

## Practical Examples

### Leaderboard

```python
users = (
    await User.objects.annotate(
        score=Sum("achievements__points"),
    )
    .order_by("-score")
    .limit(10)
)

for rank, user in enumerate(users, 1):
    print(f"#{rank} {user.name}: {user.score} points")
```

### Revenue Report

```python
report = await Order.objects.filter(
    created_at__year=2024,
).aggregate(
    total_orders=Count("id"),
    total_revenue=Sum("amount"),
    avg_order_value=Avg("amount"),
    max_order=Max("amount"),
    min_order=Min("amount"),
)

print(f"Orders: {report['total_orders']}")
print(f"Revenue: ${report['total_revenue']:,.2f}")
print(f"AOV: ${report['avg_order_value']:,.2f}")
```

### Monthly Revenue Breakdown

```python
# Using conditional aggregation for monthly breakdown
monthly = await Order.objects.filter(
    created_at__year=2024,
).aggregate(
    jan=Sum(Case(When(created_at__month=1, then=F("amount")))),
    feb=Sum(Case(When(created_at__month=2, then=F("amount")))),
    mar=Sum(Case(When(created_at__month=3, then=F("amount")))),
    # ... and so on
)
```

### Atomic Counter Update

```python
# Thread-safe page view counter -- uses SQL UPDATE, no SELECT needed
await PageView.objects.filter(
    page_id=page.id,
    date=today,
).update(count=F("count") + 1)
```

### Inventory Alert

```python
# Products running low vs well-stocked
inventory = await Product.objects.annotate(
    stock_status=Case(
        When(stock__lte=0, then=Value("out_of_stock")),
        When(stock__lt=F("reorder_level"), then=Value("low")),
        When(stock__lt=F("reorder_level") * 2, then=Value("ok")),
        default=Value("well_stocked"),
    ),
).all()

out_of_stock = [p for p in inventory if p.stock_status == "out_of_stock"]
low_stock = [p for p in inventory if p.stock_status == "low"]
```

### User Engagement Scoring

```python
users = (
    await User.objects.annotate(
        engagement_score=(
            F("login_count") * Value(1)
            + F("post_count") * Value(5)
            + F("comment_count") * Value(2)
            + F("share_count") * Value(3)
        ),
        tier=Case(
            When(engagement_score__gt=1000, then=Value("power_user")),
            When(engagement_score__gt=100, then=Value("active")),
            When(engagement_score__gt=10, then=Value("casual")),
            default=Value("inactive"),
        ),
    )
    .order_by("-engagement_score")
    .all()
)
```

### Statistical Analysis

```python
# Standard deviation and variance for quality control
stats = await Measurement.objects.filter(
    batch_id=batch.id,
).aggregate(
    avg_value=Avg("value"),
    std_dev=StdDev("value"),
    variance=Variance("value"),
    min_value=Min("value"),
    max_value=Max("value"),
    sample_size=Count("id"),
)

# Check if process is within control limits
if stats["std_dev"] > acceptable_threshold:
    await alert_quality_team(batch.id, stats)
```

### Window Functions via Raw SQL

For window functions not yet wrapped as expressions, use raw SQL:

```python
from hyperdjango.database import get_db

db = get_db()
rows = await db.query("""
    SELECT
        id,
        name,
        department,
        salary,
        ROW_NUMBER() OVER (PARTITION BY department ORDER BY salary DESC) as rank,
        AVG(salary) OVER (PARTITION BY department) as dept_avg,
        salary - AVG(salary) OVER (PARTITION BY department) as diff_from_avg
    FROM employees
    WHERE active = true
    ORDER BY department, rank
""")
```

### Subquery via Raw SQL

For complex subquery patterns:

```python
db = get_db()
rows = await db.query(
    """
    SELECT
        c.id,
        c.name,
        (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) as order_count,
        (SELECT MAX(o.created_at) FROM orders o WHERE o.customer_id = c.id) as last_order
    FROM customers c
    WHERE EXISTS (
        SELECT 1 FROM orders o
        WHERE o.customer_id = c.id
        AND o.created_at > $1
    )
    ORDER BY order_count DESC
""",
    (cutoff_date,),
)
```

## Expression Internals

All expressions implement the `Expression` base class:

```python
class Expression:
    def as_sql(self, param_offset: int = 0) -> tuple[str, list[Any]]:
        """Generate SQL string and parameter list."""
        ...

    @property
    def default_alias(self) -> str:
        """Default column alias when used without explicit name."""
        ...

    @property
    def contains_aggregate(self) -> bool:
        """True if this expression contains an aggregate function."""
        ...
```

Expressions compose via operators -- `F("x") + F("y")` creates a `CombinedExpression(F("x"), "+", F("y"))`. Parameters are tracked with `$N` placeholders for PostgreSQL and offset correctly when combining multiple expressions.

## Reference

### Imports

```python
from hyperdjango.expressions import (
    # Column reference
    F,
    # Literal value
    Value,
    # Aggregates
    Count,
    Sum,
    Avg,
    Max,
    Min,
    StdDev,
    Variance,
    # Conditional
    Case,
    When,
    # Utility
    Coalesce,
    Cast,
    Subquery,
    # Base classes (for custom expressions)
    Expression,
    Aggregate,
    CombinedExpression,
    NegatedExpression,
)
```

### F Operations

| Operation      | Example           | SQL        |
| -------------- | ----------------- | ---------- |
| Add            | `F("a") + F("b")` | `(a + b)`  |
| Subtract       | `F("a") - F("b")` | `(a - b)`  |
| Multiply       | `F("a") * 2`      | `(a * $1)` |
| Divide         | `F("a") / F("b")` | `(a / b)`  |
| Modulo         | `F("a") % 10`     | `(a % $1)` |
| Negate         | `-F("a")`         | `(-a)`     |
| Right-hand ops | `100 - F("a")`    | `($1 - a)` |

### Aggregate Parameters

| Parameter     | Type                | Default  | Description               |
| ------------- | ------------------- | -------- | ------------------------- |
| `expression`  | `str \| Expression` | required | Column name or expression |
| `distinct`    | `bool`              | `False`  | COUNT/SUM/AVG DISTINCT    |
| `filter_expr` | `dict \| None`      | `None`   | FILTER (WHERE ...) clause |

## Q Objects

`Q()` enables complex query conditions with AND, OR, and NOT operators. Use Q objects when you need conditions that can't be expressed with keyword-only `filter()`.

```python
from hyperdjango.expressions import Q
```

### OR Conditions

```python
# Users who are admin OR moderator
users = await User.objects.filter(Q(role="admin") | Q(role="moderator")).all()
# SQL: WHERE (role = $1 OR role = $2)
```

### AND + NOT

```python
# Active, non-banned users
users = await User.objects.filter(Q(is_active=True) & ~Q(is_banned=True)).all()
# SQL: WHERE (is_active = $1 AND NOT (is_banned = $2))
```

### Nested Composition

```python
# (Python OR Web) AND published
posts = await Post.objects.filter(
    (Q(category="python") | Q(category="web")) & Q(published=True)
).all()
# SQL: WHERE ((category = $1 OR category = $2) AND published = $3)
```

### Mixed Q + Keyword Args

Positional Q objects and keyword arguments are ANDed together:

```python
# Q for OR + keyword for AND
posts = await Post.objects.filter(
    Q(title__icontains="guide") | Q(title__icontains="tutorial"),
    published=True,
).all()
# SQL: WHERE (title ILIKE $1 OR title ILIKE $2) AND published = $3
```

### Q in exclude()

```python
# Exclude banned OR suspended users
active = await User.objects.exclude(Q(status="banned") | Q(status="suspended")).all()
# SQL: WHERE NOT ((status = $1 OR status = $2))
```

### All Lookups Work

Q supports every lookup and FK-spanning filter:

```python
Q(age__gte=18)  # age >= $1
Q(name__icontains="alice")  # name ILIKE $1
Q(tags__in=["python", "web"])  # tags = ANY($1)
Q(author__name__startswith="A")  # FK spanning
Q(created__year=2024)  # Transform
```

### Operators

| Operator | Meaning | Example            |
| -------- | ------- | ------------------ |
| `\|`     | OR      | `Q(a=1) \| Q(b=2)` |
| `&`      | AND     | `Q(a=1) & Q(b=2)`  |
| `~`      | NOT     | `~Q(deleted=True)` |
