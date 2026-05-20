"""
Seed data for the semantic search example.

Seeds articles with real dense embeddings from the configured OpenAI-compatible API.
Batch-embeds for efficiency (up to 100 texts per API call).

Requires EMBEDDINGS_API_KEY to be set.

Called by: uv run hyper setup --app services.semantic_search.app:app --seed services.semantic_search.seed:run
"""

import random
from datetime import UTC, datetime, timedelta

from hyperdjango.auth import hash_password, seed_password
from hyperdjango.auth.permissions import PermissionChecker
from hyperdjango.auth.user import ensure_rbac_tables
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from services.semantic_search.app import (
    EMBEDDINGS_API_KEY,
    Article,
    Category,
    User,
    embed_texts,
)

# 30 hand-written articles showcasing HyperDjango features
CURATED_ARTICLES = [
    (
        "Introduction to Python Web Frameworks",
        "Python web frameworks like Django and Flask make building web applications fast. HyperDjango extends this with native Zig performance.",
        "web",
    ),
    (
        "PostgreSQL Performance Tuning",
        "Optimize PostgreSQL with indexing, query planning, and connection pooling. pg.zig provides 2x throughput over psycopg3.",
        "database",
    ),
    (
        "Building REST APIs",
        "REST APIs use HTTP methods for CRUD operations. HyperDjango's REST framework provides ModelSerializer, ViewSet, and pagination.",
        "api",
    ),
    (
        "Machine Learning with Neural Networks",
        "Deep learning uses neural networks to learn complex patterns. Embeddings transform text into dense vector representations.",
        "ml",
    ),
    (
        "Natural Language Processing",
        "NLP combines machine learning with language understanding. pgvector stores embeddings with HNSW indexes for fast search.",
        "ml",
    ),
    (
        "Container Deployment with Docker",
        "Docker containers package applications with dependencies. Kubernetes orchestrates deployment and scaling.",
        "devops",
    ),
    (
        "Database Migration Strategies",
        "Schema migrations track database changes. HyperDjango supports squashing, offline SQL generation, and async runners.",
        "database",
    ),
    (
        "Async Python and Concurrency",
        "Python asyncio enables concurrent I/O. HyperDjango's Zig server runs 24 OS threads without the GIL at 13K req/s.",
        "python",
    ),
    (
        "Web Security Best Practices",
        "Protect applications with HTTPS, CSRF tokens, and input validation. Use argon2id for password hashing.",
        "security",
    ),
    (
        "Vector Search and Similarity",
        "pgvector adds vector types and HNSW indexes to PostgreSQL. HyperDjango provides VectorField and native Zig SIMD ops.",
        "ml",
    ),
    (
        "Caching Strategies",
        "Multi-tier caching with L1 memory and L2 database caches. HyperDjango provides TwoTierCache and ConsistentHashRing.",
        "web",
    ),
    (
        "Monitoring and Observability",
        "Prometheus metrics and structured logging provide production visibility. hyperdjango.telemetry exports native counters + histograms to /metrics.",
        "devops",
    ),
    (
        "GraphQL vs REST",
        "GraphQL lets clients request exact data. HyperDjango's REST framework includes HMAC-signed cursor pagination.",
        "api",
    ),
    (
        "Python Type Annotations",
        "Type hints enable static analysis. HyperDjango's Zig validator processes 1.6M models/sec with SIMD batch validation.",
        "python",
    ),
    (
        "SQL Query Optimization",
        "Proper indexes and query rewrites improve performance. DataLoader batches async lookups to prevent N+1 queries.",
        "database",
    ),
    (
        "Template Engine Performance",
        "HyperDjango's Zig template engine renders in 41us cached, 220x faster compile than Jinja2.",
        "web",
    ),
    (
        "OAuth2 Authentication",
        "OAuth2 with authorization codes and PKCE. HyperDjango supports Google, GitHub, and Auth0 providers.",
        "security",
    ),
    (
        "Time Series Analytics",
        "PostgreSQL range types and window functions for analytics. HyperDjango's metering engine tracks multi-dimensional usage.",
        "database",
    ),
    (
        "Microservices Communication",
        "REST APIs, message queues, and gRPC. HyperDjango channels provide pub/sub over PostgreSQL LISTEN/NOTIFY.",
        "devops",
    ),
    (
        "Full-Text Search",
        "PostgreSQL tsvector/tsquery with GIN indexes. Combine with pgvector for hybrid keyword plus semantic search.",
        "database",
    ),
    (
        "Rate Limiting",
        "Per-IP, per-user, per-tier limits. HyperDjango's rule-based rate limiter supports path patterns and cost multipliers.",
        "security",
    ),
    (
        "WebSocket Communication",
        "HyperDjango's Zig server handles WebSocket upgrades with SIMD XOR unmasking. Channels provide pub/sub rooms.",
        "web",
    ),
    (
        "Connection Pool Management",
        "pg.zig pool provides thread-owned pinning, prepared statement caching, and background health heartbeats.",
        "database",
    ),
    (
        "Admin Interface Generation",
        "HyperAdmin provides CRUD views, search, filters, bulk actions, inline editing, RBAC, and dark mode themes.",
        "web",
    ),
    (
        "Multi-Tenant Architecture",
        "Row-level isolation with automatic query filtering. TenantMixin auto-injects tenant context.",
        "api",
    ),
    (
        "Embedding Models",
        "OpenAI text-embedding-3-small produces 1536-dim vectors. HNSW indexes trade recall for speed at scale.",
        "ml",
    ),
    (
        "Database Backup and Recovery",
        "PostgreSQL point-in-time recovery with WAL archiving. Test recovery procedures before production.",
        "database",
    ),
    (
        "CI/CD Pipelines",
        "HyperDjango's benchmark command detects query regressions in CI with EXPLAIN ANALYZE comparison.",
        "devops",
    ),
    (
        "Form Validation",
        "12 field types, cross-field clean methods, and ModelForm auto-generation from Model definitions.",
        "web",
    ),
    (
        "Structured Logging",
        "HyperDjango's loguru-compatible logger with non-blocking background writing and Zig-accelerated timestamps.",
        "devops",
    ),
]

# Templates for generating additional articles
_TEMPLATES = [
    "Understanding {topic} in {domain}",
    "A Guide to {topic} with {tool}",
    "Best Practices for {topic}",
    "How {topic} Improves {domain}",
    "{topic} Patterns for {domain}",
    "Advanced {topic} Techniques",
    "Getting Started with {topic}",
    "Scaling with {topic}",
    "{topic} Architecture Guide",
    "Practical {topic} for Engineers",
]

_TOPICS = [
    "Connection Pooling",
    "Query Caching",
    "Schema Design",
    "Index Optimization",
    "Error Handling",
    "Input Validation",
    "Rate Limiting",
    "Authentication",
    "Data Modeling",
    "API Versioning",
    "Load Testing",
    "Memory Management",
    "Configuration",
    "Health Checks",
    "Graceful Shutdown",
    "Batch Processing",
    "Event Sourcing",
    "Dependency Injection",
    "Middleware Chains",
    "Response Caching",
    "ORM Queries",
    "Prepared Statements",
    "SSL Configuration",
    "CORS Setup",
    "Password Security",
    "Session Management",
    "Token Rotation",
    "Vector Indexing",
    "Embedding Generation",
    "Similarity Search",
    "Model Serving",
    "Data Pipelines",
]

_DOMAINS = [
    "Web Applications",
    "API Services",
    "Database Systems",
    "Cloud Infrastructure",
    "Machine Learning",
    "DevOps",
    "Security Engineering",
    "Python Development",
]

_TOOLS = ["PostgreSQL", "Python", "Docker", "Kubernetes", "Prometheus", "HyperDjango"]

_BODIES = [
    "Reduces latency by eliminating unnecessary database round-trips.",
    "Proper configuration ensures the system handles peak traffic.",
    "Monitoring key metrics helps identify bottlenecks early.",
    "Automated testing catches regressions in the development cycle.",
    "Security best practices protect against injection and XSS attacks.",
    "Connection pooling amortizes the cost of establishing connections.",
    "Async processing handles thousands of concurrent requests.",
    "Index selection dramatically affects query performance.",
    "Cache invalidation balances freshness with performance.",
    "Structured logging enables efficient searching in production.",
    "Template compilation at startup eliminates per-request overhead.",
    "Rate limiting protects services from abuse.",
    "Schema migrations enable safe rollbacks when issues arise.",
    "Health endpoints enable load balancers to route traffic correctly.",
    "Graceful shutdown ensures in-flight requests complete.",
    "Background tasks offload expensive computations.",
    "HNSW indexes provide sub-millisecond nearest neighbor search.",
    "Password hashing with argon2id resists brute-force attacks.",
    "Session-based auth avoids the security issues of JWT tokens.",
    "Vector similarity search finds semantically related content.",
]

BATCH_SIZE = 100  # Max texts per API call


async def _embed_and_insert(
    db, articles: list[tuple[str, str, str]], author_id: int
) -> None:
    """Batch-embed articles and insert them into the database.

    Args:
        articles: list of (title, body, category) tuples
        author_id: user ID for the author_id column
    """
    # Build texts for embedding: title + body for each article
    texts = [f"{title} {body}" for title, body, _ in articles]

    # Batch embed in chunks
    all_vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        chunk = texts[i : i + BATCH_SIZE]
        vectors = await embed_texts(chunk)
        all_vectors.extend(vectors)

    # Insert articles with embeddings via ORM. Platform rule: seeds
    # MUST use Model(...).save() so TimestampMixin, field validation,
    # and enum coercion all run consistently. The ORM now handles
    # VectorField serialization natively (v0.14.18) — pass the raw
    # list[float] directly, and Model._insert formats it as pgvector
    # bracket literal via the precomputed `meta.vector_columns` set.
    # The realistic-spread created_at (up to 90 days in the past) is
    # replicated by passing an explicit datetime — TimestampMixin
    # respects pre-set values and only fills None on first save.
    now = datetime.now(UTC)
    for (title, body, category), vec in zip(articles, all_vectors):
        created_at = now - timedelta(days=random.randint(0, 90))
        article = Article(
            title=title,
            body=body,
            category=category,
            author_id=author_id,
            embedding=vec,
            created_at=created_at,
        )
        await article.save()


async def run(db=None) -> None:
    """Seed the semantic search database with articles + real embeddings."""
    if db is None:
        db = get_db()

    # Demo user — always created (needed for auth tests even without API key)
    demo = await User.objects.filter(username="demo").first()
    if demo is None:
        demo = User(username="demo", password_hash=hash_password(seed_password("demo")))
        await demo.save()
        logger.info(
            "  vs_users: demo user created — see seed_password log for actual value"
        )

    demo_id = demo.id

    # HyperAdmin panel user (hyper_users table, separate from app users)
    await ensure_rbac_tables(db=db)
    checker = PermissionChecker(db)
    await checker.ensure_admin_user()
    logger.info("    hyper_users: admin ensured for HyperAdmin panel")

    if not EMBEDDINGS_API_KEY:
        logger.warning(
            "EMBEDDINGS_API_KEY not set — skipping article seeding. "
            "Set it and re-run: export EMBEDDINGS_API_KEY=sk-..."
        )
        return

    # Check if already seeded
    count = await Article.objects.count()
    target = len(CURATED_ARTICLES) + 970
    if count >= target:
        logger.info("  vs_articles: already seeded ({count} articles)", count=count)
        return
    if count > 0:
        logger.info(
            "  vs_articles: partial seed ({count}/{target}), clearing...",
            count=count,
            target=target,
        )
        await Article.objects.delete()

    # Seed curated articles
    logger.info(
        "  Embedding and seeding {n} curated articles...", n=len(CURATED_ARTICLES)
    )
    await _embed_and_insert(db, list(CURATED_ARTICLES), demo_id)

    # Generate additional articles
    gen_count = target - len(CURATED_ARTICLES)
    logger.info("  Generating {n} additional articles...", n=gen_count)
    rng = random.Random(42)

    generated: list[tuple[str, str, str]] = []
    for _ in range(gen_count):
        topic = rng.choice(_TOPICS)
        domain = rng.choice(_DOMAINS)
        tool = rng.choice(_TOOLS)
        title = rng.choice(_TEMPLATES).format(topic=topic, domain=domain, tool=tool)
        body = " ".join(rng.sample(_BODIES, k=rng.randint(3, 5)))
        category = rng.choice([c.value for c in Category])
        generated.append((title, body, category))

    logger.info(
        "  Embedding {n} articles via API (batch size {bs})...",
        n=len(generated),
        bs=BATCH_SIZE,
    )
    await _embed_and_insert(db, generated, demo_id)

    await db.execute("ANALYZE vs_articles")
    total = await Article.objects.count()
    logger.success(
        "  vs_articles: {total} articles seeded with real embeddings", total=total
    )
