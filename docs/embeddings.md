# Vector Embeddings (pgvector)

HyperDjango provides first-class pgvector integration for storing and searching vector embeddings directly in PostgreSQL. Combine the full power of a relational database with sub-millisecond approximate nearest neighbor (ANN) search -- no separate vector database needed.

## Why Vector Search

Vector embeddings are fixed-length float arrays that encode semantic meaning. Text, images, and other unstructured data can be projected into vector space by an embedding model (OpenAI, Cohere, sentence-transformers, etc.), then searched by geometric distance. Use cases include:

- **Semantic search** -- find documents by meaning, not keywords
- **Retrieval-augmented generation (RAG)** -- feed relevant context into an LLM
- **Recommendation engines** -- find items similar to a user's history
- **Deduplication** -- detect near-duplicate content
- **Clustering** -- group similar records without manual labels

pgvector stores vectors alongside your relational data in the same transaction, same backup, same replication stream. No ETL pipeline to a separate system.

## Installation

Enable the pgvector extension in your PostgreSQL database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Requires PostgreSQL 15+ and pgvector 0.5+. pgvector ships with most managed PostgreSQL providers (Supabase, Neon, RDS, Cloud SQL).

HyperDjango's pg.zig driver automatically registers the `vector` type OID at connection time, so vector columns are decoded natively in the binary protocol path -- no Python-side parsing overhead.

## VectorField

`VectorField` declares a pgvector `vector(N)` column on a model. Import it alongside `Model` and `Field`:

```python
from hyperdjango import Model, Field
from hyperdjango.models import VectorField


class Document(Model):
    class Meta:
        table = "documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    content: str = Field(default="")
    embedding: list[float] = VectorField(dimensions=1536)
```

### Constructor Parameters

| Parameter      | Type             | Default               | Description                                               |
| -------------- | ---------------- | --------------------- | --------------------------------------------------------- |
| `dimensions`   | `int`            | `1536`                | Fixed vector length. Must match your embedding model.     |
| `index_type`   | `str`            | `"hnsw"`              | Index algorithm: `"hnsw"` or `"ivfflat"`.                 |
| `index_ops`    | `str`            | `"vector_cosine_ops"` | Operator class for the distance metric (see table below). |
| `index`        | `bool`           | `True`                | Whether to create a vector index on this column.          |
| `index_params` | `dict[str, int]` | `None`                | Index tuning parameters for the WITH clause (see below).  |

### Operator Classes

| `index_ops` value     | Distance metric         | pgvector operator | Best for                          |
| --------------------- | ----------------------- | ----------------- | --------------------------------- |
| `"vector_cosine_ops"` | Cosine distance         | `<=>`             | Normalized embeddings (most APIs) |
| `"vector_l2_ops"`     | Euclidean (L2) distance | `<->`             | Spatial / geometric data          |
| `"vector_ip_ops"`     | Negative inner product  | `<#>`             | Maximum inner product search      |

### Common Dimension Values

| Embedding model                 | Dimensions |
| ------------------------------- | ---------- |
| OpenAI `text-embedding-ada-002` | 1536       |
| OpenAI `text-embedding-3-small` | 1536       |
| OpenAI `text-embedding-3-large` | 3072       |
| Cohere `embed-english-v3.0`     | 1024       |
| `all-MiniLM-L6-v2` (SBERT)      | 384        |
| `all-mpnet-base-v2` (SBERT)     | 768        |

```python
# 384-dim model with L2 distance
embedding: list[float] = VectorField(
    dimensions=384,
    index_ops="vector_l2_ops",
)

# 3072-dim model, IVFFlat index, no auto-index
embedding: list[float] = VectorField(
    dimensions=3072,
    index_type="ivfflat",
    index=False,
)

# HNSW with tuning parameters (generates WITH clause in DDL)
embedding: list[float] = VectorField(
    dimensions=1536,
    index_params={"m": 16, "ef_construction": 64},
)
# → CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
```

### Index Tuning Parameters

The `index_params` dict is passed as a `WITH (...)` clause on the index DDL. Common parameters:

| Index type | Parameter         | Default | Description                                                    |
| ---------- | ----------------- | ------- | -------------------------------------------------------------- |
| HNSW       | `m`               | 16      | Max connections per node (higher = better recall, more memory) |
| HNSW       | `ef_construction` | 64      | Build-time beam width (higher = better quality, slower build)  |
| IVFFlat    | `lists`           | 100     | Number of inverted list partitions                             |

## Distance Lookups

HyperDjango registers four pgvector lookups on `QuerySet.filter()`. Each takes a tuple of `(query_vector, threshold_or_metric)`.

### cosine_distance

Filter rows where cosine distance to the query vector is below a threshold. Cosine distance ranges from 0 (identical) to 2 (opposite).

```python
query_vec = await get_embedding("machine learning tutorial")

# Find documents within cosine distance 0.3
docs = await Document.objects.filter(embedding__cosine_distance=(query_vec, 0.3)).all()
```

Generated SQL:

```sql
SELECT * FROM documents WHERE embedding <=> $1::vector < $2
-- $1 = '[0.012, -0.045, ...]', $2 = 0.3
```

### l2_distance

Filter by Euclidean (L2) distance. Useful for spatial or geometric embeddings.

```python
nearby = await Point.objects.filter(embedding__l2_distance=(query_vec, 1.5)).all()
```

Generated SQL:

```sql
SELECT * FROM points WHERE embedding <-> $1::vector < $2
```

### inner_product

Filter by negative inner product. pgvector's `<#>` operator returns the _negative_ inner product, so smaller values mean higher similarity.

```python
similar = await Item.objects.filter(embedding__inner_product=(query_vec, -0.8)).all()
```

Generated SQL:

```sql
SELECT * FROM items WHERE embedding <#> $1::vector < $2
```

### nearest

Order results by distance for K-nearest-neighbor queries. Pair with `.limit()` to get the top-K results. The second element of the tuple selects the distance metric: `"cosine"`, `"l2"`, or `"inner_product"`.

```python
# Top 10 nearest by cosine distance
top_10 = (
    await Document.objects.filter(embedding__nearest=(query_vec, "cosine"))
    .limit(10)
    .all()
)

# Top 5 nearest by L2 distance
top_5 = (
    await Document.objects.filter(embedding__nearest=(query_vec, "l2")).limit(5).all()
)
```

The `nearest` lookup produces an `IS NOT NULL` condition (always true for non-null vectors) combined with an `ORDER BY` clause using the selected distance operator. The actual filtering happens via the `LIMIT`.

## Vector Indexes

Indexing is critical for vector search performance. Without an index, pgvector scans every row (exact search). With an index, it uses approximate nearest neighbor algorithms that trade a small amount of recall for orders-of-magnitude speed improvement.

### HNSW vs IVFFlat

| Property             | HNSW                           | IVFFlat                           |
| -------------------- | ------------------------------ | --------------------------------- |
| Query speed          | Faster                         | Slower                            |
| Build speed          | Slower                         | Faster                            |
| Memory usage         | Higher                         | Lower                             |
| Recall at same speed | Higher                         | Lower                             |
| Insert performance   | Good (no rebuild needed)       | Requires periodic re-clustering   |
| Best for             | Most workloads, real-time apps | Large static datasets, bulk loads |

**Default recommendation:** Use HNSW unless you have a specific reason to use IVFFlat (e.g., very large datasets where build time matters more than query latency).

### CreateVectorIndex

The migration system provides `CreateVectorIndex` for managing vector indexes:

```python
from hyperdjango.migrations import CreateVectorIndex

# HNSW index (default)
CreateVectorIndex(
    table="documents",
    column="embedding",
    index_type="hnsw",
    index_ops="vector_cosine_ops",
    m=16,  # Max connections per layer (default: 16)
    ef_construction=64,  # Construction search width (default: 64)
)

# IVFFlat index
CreateVectorIndex(
    table="documents",
    column="embedding",
    index_type="ivfflat",
    index_ops="vector_cosine_ops",
    lists=100,  # Number of IVF lists (default: 100)
)
```

### CreateVectorIndex Parameters

| Parameter         | Type  | Default               | Description                                                 |
| ----------------- | ----- | --------------------- | ----------------------------------------------------------- |
| `table`           | `str` | --                    | Table name.                                                 |
| `column`          | `str` | --                    | Column name.                                                |
| `index_type`      | `str` | `"hnsw"`              | `"hnsw"` or `"ivfflat"`.                                    |
| `index_ops`       | `str` | `"vector_cosine_ops"` | Operator class matching the distance metric you query with. |
| `m`               | `int` | `16`                  | HNSW only. Max connections per node per layer.              |
| `ef_construction` | `int` | `64`                  | HNSW only. Size of the dynamic candidate list during build. |
| `lists`           | `int` | `100`                 | IVFFlat only. Number of inverted lists (clusters).          |

Generated SQL for HNSW:

```sql
CREATE INDEX idx_documents_embedding_hnsw ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

Generated SQL for IVFFlat:

```sql
CREATE INDEX idx_documents_embedding_ivfflat ON documents
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

Rollback automatically drops the index:

```sql
DROP INDEX IF EXISTS idx_documents_embedding_hnsw;
```

### Tuning HNSW Parameters

- **`m`** (default 16): Higher values improve recall but increase memory and build time. Range: 4--64. Use 32--48 for high-recall requirements.
- **`ef_construction`** (default 64): Higher values improve index quality at the cost of build time. Range: 16--512. Use 128--256 for production datasets.
- **`ef_search`** (runtime): Set via `SET hnsw.ef_search = 100` before querying. Higher values improve recall at query time. Default is 40.

### Tuning IVFFlat Parameters

- **`lists`** (default 100): Number of clusters. Rule of thumb: `sqrt(num_rows)` for up to 1M rows, `sqrt(num_rows / 1000)` for larger datasets.
- **`probes`** (runtime): Set via `SET ivfflat.probes = 10` before querying. Higher values check more lists, improving recall. Default is 1.

## Examples

### Semantic Search

Store document embeddings and search by meaning:

```python
from hyperdjango import Model, Field
from hyperdjango.models import VectorField


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    body: str = Field(default="")
    embedding: list[float] = VectorField(dimensions=1536)


async def search_articles(query: str, limit: int = 10) -> list[Article]:
    query_vec = await embed(query)  # Your embedding function

    return (
        await Article.objects.filter(embedding__nearest=(query_vec, "cosine"))
        .limit(limit)
        .all()
    )


async def find_similar(article: Article, limit: int = 5) -> list[Article]:
    return (
        await Article.objects.filter(embedding__nearest=(article.embedding, "cosine"))
        .exclude(id=article.id)
        .limit(limit)
        .all()
    )
```

### RAG Pattern

Retrieve relevant context and feed it to an LLM:

```python
async def answer_question(question: str) -> str:
    # 1. Embed the question
    query_vec = await embed(question)

    # 2. Retrieve top-5 most relevant chunks
    chunks = (
        await Chunk.objects.filter(embedding__cosine_distance=(query_vec, 0.5))
        .limit(5)
        .all()
    )

    # 3. Build context
    context = "\n\n".join(chunk.text for chunk in chunks)

    # 4. Generate answer with context
    return await llm_complete(
        system="Answer based on the provided context.",
        user=f"Context:\n{context}\n\nQuestion: {question}",
    )
```

### Recommendation Engine

Recommend products based on a user's purchase history:

```python
class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    description: str = Field(default="")
    embedding: list[float] = VectorField(dimensions=768)


async def recommend_for_user(user_id: int, limit: int = 10) -> list[Product]:
    # Get embeddings of user's recent purchases
    purchased = await db.query(
        "SELECT p.embedding FROM products p "
        "JOIN orders o ON o.product_id = p.id "
        "WHERE o.user_id = $1 ORDER BY o.created_at DESC LIMIT 5",
        user_id,
    )

    if not purchased:
        return []

    # Average the purchase embeddings into a user preference vector
    dims = len(purchased[0]["embedding"])
    avg_vec = [0.0] * dims
    for row in purchased:
        for i, v in enumerate(row["embedding"]):
            avg_vec[i] += v
    avg_vec = [v / len(purchased) for v in avg_vec]

    # Find nearest products (excluding already purchased)
    purchased_ids = await db.query(
        "SELECT product_id FROM orders WHERE user_id = $1", user_id
    )
    exclude_ids = [r["product_id"] for r in purchased_ids]

    candidates = (
        await Product.objects.filter(embedding__nearest=(avg_vec, "cosine"))
        .limit(limit + len(exclude_ids))
        .all()
    )

    return [p for p in candidates if p.id not in exclude_ids][:limit]
```

## Performance

### SIMD Binary Decode

pg.zig decodes vector columns directly from the PostgreSQL binary wire format using SIMD instructions. A 1536-dimensional vector (6 KB on the wire) is decoded without Python-side float parsing -- the raw IEEE 754 float32 bytes are read directly into a Python list.

### Vector Formatting

Vectors are formatted for PostgreSQL using `_format_vector()`, which converts a Python `list[float]` to pgvector's text format: `[0.1,0.2,0.3]`. String vectors pass through unchanged.

### Index Tuning Tips

1. **Always index vector columns in production.** Without an index, every query is an O(n) sequential scan. VectorField creates an index by default (`index=True`).

2. **Match your operator class to your queries.** If you query with `cosine_distance`, index with `vector_cosine_ops`. Mismatched operator classes force sequential scans.

3. **Build IVFFlat indexes after loading data.** IVFFlat clusters the data during index creation. Building on an empty table produces a useless index. HNSW does not have this limitation.

4. **Increase `ef_search` for higher recall.** The default `hnsw.ef_search = 40` is tuned for speed. For applications where missing a relevant result is costly (RAG, medical search), increase to 100--200:

   ```sql
   SET hnsw.ef_search = 200;
   ```

5. **Use `EXPLAIN ANALYZE` to verify index usage.** If you see `Seq Scan` instead of `Index Scan`, check that your operator class matches and that the table has been `VACUUM`ed after bulk inserts.

6. **Normalize your vectors.** Cosine distance on normalized (unit-length) vectors is equivalent to inner product distance, letting you use either operator interchangeably. Most embedding APIs return normalized vectors by default.

7. **Batch your embedding API calls.** The database query is fast (sub-millisecond with an index). The bottleneck is usually the embedding API call. Batch multiple texts into a single API request where possible.
