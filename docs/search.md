# Search

Full-text search using PostgreSQL's native search capabilities -- no external search engine needed. HyperDjango supports full-text search with ranking, highlighting, language-aware stemming, and trigram-based fuzzy matching, all backed by GIN indexes for sub-millisecond query times on millions of rows.

## Quick Start

```python
# 1. Add a tsvector column (auto-updated on INSERT/UPDATE)
await db.execute("""
    ALTER TABLE articles ADD COLUMN IF NOT EXISTS
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED
""")

# 2. Create a GIN index for fast search
await db.execute(
    "CREATE INDEX IF NOT EXISTS idx_search ON articles USING gin(search_vector)"
)


# 3. Search
@app.get("/search")
async def search(request):
    q = request.query_params.get("q", [""])[0]
    if not q:
        return {"results": []}

    results = await db.query(
        "SELECT id, title, ts_rank(search_vector, query) as rank "
        "FROM articles, plainto_tsquery('english', $1) query "
        "WHERE search_vector @@ query "
        "ORDER BY rank DESC LIMIT 20",
        q,
    )
    return {"results": results}
```

## How PostgreSQL Full-Text Search Works

PostgreSQL full-text search operates on two data types:

- **tsvector**: A sorted list of distinct lexemes (normalized words) extracted from a document. Each lexeme includes position and weight information.
- **tsquery**: A search query containing lexemes combined with boolean operators (`&` AND, `|` OR, `!` NOT, `<->` phrase).

The `@@` operator matches a tsvector against a tsquery. Combined with a GIN index, this provides fast full-text search without table scans.

### Creating a tsvector Column

**Option 1: GENERATED ALWAYS (recommended)**

The column auto-updates whenever the source columns change:

```sql
ALTER TABLE articles ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED;
```

**Option 2: Manual trigger-based updates**

For more control over when the vector is updated:

```sql
ALTER TABLE articles ADD COLUMN search_vector tsvector;

-- Update manually after INSERT/UPDATE
UPDATE articles SET search_vector =
    to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''));
```

**Option 3: Weighted vectors (see Weighted Search section below)**

### Creating the GIN Index

```sql
CREATE INDEX idx_articles_search ON articles USING gin(search_vector);
```

This index makes `@@` queries fast. Without it, PostgreSQL must scan every row.

## Search Functions

PostgreSQL provides three functions for converting user input into a tsquery.

### plainto_tsquery

Simple word-based search. All words are combined with AND. No special syntax is supported -- user input is treated as plain text.

```python
# Matches articles containing BOTH "python" AND "web"
results = await db.query(
    "SELECT * FROM articles WHERE search_vector @@ plainto_tsquery('english', $1)",
    "python web",
)
```

| User Input       | tsquery Generated  | Matches                     |
| ---------------- | ------------------ | --------------------------- |
| `"python web"`   | `'python' & 'web'` | Must contain both           |
| `"running fast"` | `'run' & 'fast'`   | Stemmed: "running" -> "run" |
| `"the a an"`     | (empty)            | Stopwords removed           |

**Best for:** Search boxes where users type natural language queries.

### to_tsquery

Full query syntax with boolean operators. The input must use the operator syntax directly.

```python
# OR: matches "python" or "javascript"
results = await db.query(
    "SELECT * FROM articles WHERE search_vector @@ to_tsquery('english', $1)",
    "python | javascript",
)
```

**Operators:**

| Operator | Meaning                     | Example                        |
| -------- | --------------------------- | ------------------------------ |
| `&`      | AND                         | `python & web`                 |
| `\|`     | OR                          | `python \| javascript`         |
| `!`      | NOT                         | `python & !beginner`           |
| `<->`    | Followed by (phrase)        | `web <-> framework`            |
| `<N>`    | Followed by with distance N | `web <2> app` (within 2 words) |
| `( )`    | Grouping                    | `(python \| ruby) & web`       |

```python
# AND: both terms required
"python & web"

# NOT: exclude a term
"python & !beginner"

# Phrase: adjacent words in order
"web <-> framework"  # "web framework" as a phrase

# Distance: words within N positions
"machine <3> learning"  # "machine" within 3 words of "learning"

# Complex: grouped boolean logic
"(python | ruby) & (web <-> framework)"
```

**Best for:** Advanced search interfaces where users can construct queries.

### websearch_to_tsquery

Google-style search syntax. Supports quoted phrases, minus for exclusion, and OR.

```python
results = await db.query(
    "SELECT * FROM articles WHERE search_vector @@ websearch_to_tsquery('english', $1)",
    'python web -beginner "async framework"',
)
```

**Syntax:**

| Syntax     | Meaning            | Example           |
| ---------- | ------------------ | ----------------- |
| word       | Must contain (AND) | `python web`      |
| `"phrase"` | Exact phrase       | `"web framework"` |
| `-word`    | Exclude            | `-beginner`       |
| `OR`       | Either term        | `python OR ruby`  |

```python
# "async framework" as phrase, must include python, exclude beginner
'python "async framework" -beginner'

# Either python or ruby, about web
"python OR ruby web"

# Quoted phrase with exclusion
'"machine learning" -tensorflow'
```

**Best for:** User-facing search boxes (most intuitive syntax).

## Search Ranking

### ts_rank

Rank results by relevance based on term frequency. Higher rank = more relevant.

```python
results = await db.query(
    "SELECT id, title, ts_rank(search_vector, query) as rank "
    "FROM articles, plainto_tsquery('english', $1) query "
    "WHERE search_vector @@ query "
    "ORDER BY rank DESC",
    "python async",
)
```

`ts_rank` considers:

- How many times the search terms appear in the document
- The weights assigned to different lexemes (A, B, C, D)
- The document length (optional normalization)

**Normalization options** (bitmask, passed as 3rd argument):

| Flag           | Value | Effect                                           |
| -------------- | ----- | ------------------------------------------------ |
| Default        | `0`   | No normalization                                 |
| Length         | `1`   | Divide rank by 1 + log(document length)          |
| Length squared | `2`   | Divide rank by document length                   |
| Mean harmonic  | `4`   | Divide by mean harmonic distance between extents |
| Unique words   | `8`   | Divide by number of unique words                 |
| Log unique     | `16`  | Divide by 1 + log(unique words)                  |
| Rank + 1       | `32`  | Divide by rank + 1                               |

```python
# Normalize by document length (so short docs aren't penalized)
"ts_rank(search_vector, query, 1) as rank"

# Combine flags with bitwise OR
"ts_rank(search_vector, query, 1|32) as rank"
```

### ts_rank_cd (Cover Density Ranking)

Cover density ranking considers the proximity of matching terms. Documents where search terms appear close together rank higher than documents where they are spread apart.

```python
results = await db.query(
    "SELECT id, title, ts_rank_cd(search_vector, query) as rank "
    "FROM articles, plainto_tsquery('english', $1) query "
    "WHERE search_vector @@ query "
    "ORDER BY rank DESC",
    "python async web",
)
```

**When to use `ts_rank_cd` vs `ts_rank`:**

- `ts_rank`: Good for general relevance. Term frequency matters.
- `ts_rank_cd`: Better when proximity matters (e.g., searching for related concepts that should appear near each other).

### Combining Rank with Other Signals

```python
# Boost recent articles
results = await db.query(
    "SELECT id, title, "
    "  ts_rank(search_vector, query) * "
    "  (1.0 / (1.0 + EXTRACT(EPOCH FROM NOW() - published_at) / 86400)) as rank "
    "FROM articles, plainto_tsquery('english', $1) query "
    "WHERE search_vector @@ query "
    "ORDER BY rank DESC LIMIT 20",
    search_term,
)

# Boost by a popularity column
"ts_rank(search_vector, query) * LOG(1 + view_count) as rank"
```

## Highlighted Results (ts_headline)

`ts_headline` returns an excerpt from the original text with search terms wrapped in configurable markers.

```python
results = await db.query(
    "SELECT title, "
    "  ts_headline('english', content, plainto_tsquery('english', $1), "
    "    'StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20') as snippet "
    "FROM articles "
    "WHERE search_vector @@ plainto_tsquery('english', $1) "
    "ORDER BY ts_rank(search_vector, plainto_tsquery('english', $1)) DESC",
    "python web",
)
# snippet: "...building <mark>web</mark> applications with <mark>Python</mark>..."
```

### ts_headline Options

All options are passed as a comma-separated string in the 4th argument:

| Option              | Default   | Description                                         |
| ------------------- | --------- | --------------------------------------------------- |
| `StartSel`          | `<b>`     | Opening tag for matched terms                       |
| `StopSel`           | `</b>`    | Closing tag for matched terms                       |
| `MaxWords`          | `35`      | Maximum words in the headline                       |
| `MinWords`          | `15`      | Minimum words in the headline                       |
| `ShortWord`         | `3`       | Words shorter than this are dropped from headlines  |
| `HighlightAll`      | `false`   | Highlight all occurrences or just the best fragment |
| `MaxFragments`      | `0`       | Maximum number of fragments (0 = single headline)   |
| `FragmentDelimiter` | `" ... "` | Text between fragments                              |

### Example Configurations

```python
# HTML highlighting
"StartSel=<mark>, StopSel=</mark>, MaxWords=50, MinWords=20"

# Terminal/plain text
"StartSel=**, StopSel=**, MaxWords=40"

# Multiple fragments with ellipsis
"StartSel=<b>, StopSel=</b>, MaxFragments=3, FragmentDelimiter= ... , MaxWords=20"

# Highlight ALL occurrences (not just best fragment)
"StartSel=<mark>, StopSel=</mark>, HighlightAll=true"
```

### Multiple Fragment Example

```python
results = await db.query(
    "SELECT title, "
    "  ts_headline('english', content, query, "
    "    'StartSel=<mark>, StopSel=</mark>, MaxFragments=3, "
    "     FragmentDelimiter= ... , MaxWords=15, MinWords=5') as snippet "
    "FROM articles, websearch_to_tsquery('english', $1) query "
    "WHERE search_vector @@ query "
    "ORDER BY ts_rank(search_vector, query) DESC",
    search_term,
)
# snippet: "...using <mark>Python</mark> for web... ...the <mark>Python</mark> ecosystem..."
```

## Search Configurations (Language Support)

PostgreSQL text search configurations define how text is tokenized and normalized. Each configuration includes a parser, dictionaries (for stemming and stopword removal), and token type mappings.

### Built-in Configurations

```python
# English (default) — applies stemming and stopword removal
to_tsvector("english", text)
# "running quickly" -> 'quick':2 'run':1

# Simple — no stemming, no stopwords, lowercase only
to_tsvector("simple", text)
# "running quickly" -> 'quickly':2 'running':1

# Available language configurations:
# danish, dutch, english, finnish, french, german, hungarian,
# italian, norwegian, portuguese, romanian, russian, spanish,
# swedish, turkish
```

### Choosing a Configuration

| Use Case               | Configuration                           |
| ---------------------- | --------------------------------------- |
| English content        | `'english'`                             |
| Multi-language content | `'simple'` (no stemming, works for all) |
| Exact token matching   | `'simple'`                              |
| Code/identifiers       | `'simple'`                              |
| French content         | `'french'`                              |

### Multi-Language Search

For applications with content in multiple languages, either:

**Option 1: Simple configuration (no stemming)**

```sql
ALTER TABLE articles ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED;
```

**Option 2: Per-row language configuration**

```sql
ALTER TABLE articles ADD COLUMN language text DEFAULT 'english';
-- Update search_vector via trigger based on the language column
```

```python
# Search using the article's language
results = await db.query(
    "SELECT * FROM articles WHERE search_vector @@ to_tsquery(language::regconfig, $1)",
    "python & web",
)
```

## Trigram Search (Fuzzy Matching)

For typo-tolerant, fuzzy search, use the `pg_trgm` extension. Trigram search splits text into groups of 3 consecutive characters and compares the overlap.

### Setup

```python
# Enable the extension (once per database)
await db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

# Create a GIN trigram index
await db.execute(
    "CREATE INDEX IF NOT EXISTS idx_title_trgm ON articles USING gin(title gin_trgm_ops)"
)
```

### Similarity Search

The `%` operator returns rows where the trigram similarity exceeds the threshold (default 0.3):

```python
results = await db.query(
    "SELECT id, title, similarity(title, $1) as sim "
    "FROM articles "
    "WHERE title % $1 "
    "ORDER BY sim DESC",
    "pythn",  # Typo — still finds "python"
)
```

### Similarity Functions

| Function                       | Description                                 |
| ------------------------------ | ------------------------------------------- |
| `similarity(a, b)`             | Trigram similarity (0.0 to 1.0)             |
| `word_similarity(a, b)`        | Best similarity between a and any word in b |
| `strict_word_similarity(a, b)` | Strict word boundary matching               |

```python
# Word similarity: good for matching a query against longer text
results = await db.query(
    "SELECT title, word_similarity($1, title) as sim "
    "FROM articles "
    "WHERE $1 <<% title "  # word_similarity operator
    "ORDER BY sim DESC",
    "pythn",
)
```

### Trigram Operators

| Operator | Description                             |
| -------- | --------------------------------------- |
| `%`      | Similarity above threshold              |
| `<<%`    | Word similarity above threshold         |
| `<<<%`   | Strict word similarity above threshold  |
| `<->`    | Distance (1 - similarity), for ORDER BY |

### Setting the Similarity Threshold

```python
# Lower threshold = more fuzzy matches (more results, less precision)
await db.execute("SET pg_trgm.similarity_threshold = 0.2")

# Higher threshold = stricter matching (fewer results, more precision)
await db.execute("SET pg_trgm.similarity_threshold = 0.5")
```

Default is 0.3. Per-session setting.

## Combined Search Pattern

The most effective search combines full-text search for relevance with trigram search for typo tolerance:

```python
@app.get("/search")
async def search(request):
    q = request.query_params.get("q", [""])[0]
    if not q or len(q) < 2:
        return {"results": [], "query": q}

    # Try full-text search first
    results = await db.query(
        "SELECT id, title, "
        "  ts_headline('english', content, query, "
        "    'StartSel=<mark>, StopSel=</mark>, MaxWords=35') as snippet, "
        "  ts_rank(search_vector, query) as rank "
        "FROM articles, websearch_to_tsquery('english', $1) query "
        "WHERE search_vector @@ query "
        "ORDER BY rank DESC "
        "LIMIT 20",
        q,
    )

    # If no full-text results, fall back to trigram (fuzzy) search
    if not results:
        results = await db.query(
            "SELECT id, title, "
            "  LEFT(content, 200) as snippet, "
            "  similarity(title, $1) as rank "
            "FROM articles "
            "WHERE title % $1 "
            "ORDER BY rank DESC "
            "LIMIT 20",
            q,
        )

    return {"results": results, "query": q}
```

### Single-Query Combined Search

For a single query that combines both approaches:

```python
results = await db.query(
    "SELECT id, title, "
    "  ts_headline('english', content, query, "
    "    'StartSel=<mark>, StopSel=</mark>, MaxWords=35') as snippet, "
    "  ts_rank(search_vector, query) + similarity(title, $1) as combined_rank "
    "FROM articles, websearch_to_tsquery('english', $1) query "
    "WHERE search_vector @@ query OR title % $1 "
    "ORDER BY combined_rank DESC "
    "LIMIT 20",
    q,
)
```

## Search Across Multiple Columns

### Concatenated tsvector

The simplest approach concatenates columns with `||`:

```sql
ALTER TABLE articles ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '')) ||
        to_tsvector('english', coalesce(content, '')) ||
        to_tsvector('english', coalesce(tags, ''))
    ) STORED;
```

### Searching with Different Configurations Per Column

```sql
ALTER TABLE articles ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '')) ||
        to_tsvector('simple', coalesce(code_snippet, ''))
    ) STORED;
```

## Weighted Search (setweight)

Assign weights (A, B, C, D) to different columns so matches in more important fields rank higher. A is the highest weight, D is the lowest.

### Setting Up Weighted Vectors

```sql
-- Can't use GENERATED ALWAYS with setweight, so use a trigger or manual update
ALTER TABLE articles ADD COLUMN search_vector tsvector;

-- Manual update (run after INSERT/UPDATE)
UPDATE articles SET search_vector =
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(content, '')), 'C') ||
    setweight(to_tsvector('english', coalesce(tags, '')), 'D');
```

### Trigger-Based Updates

```sql
CREATE OR REPLACE FUNCTION articles_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.content, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trig_articles_search
    BEFORE INSERT OR UPDATE OF title, summary, content
    ON articles
    FOR EACH ROW EXECUTE FUNCTION articles_search_trigger();
```

### How Weights Affect Ranking

```python
# ts_rank uses weights: {D, C, B, A} defaults to {0.1, 0.2, 0.4, 1.0}
# Title matches (A) rank 10x higher than content matches (D)

results = await db.query(
    "SELECT title, ts_rank(search_vector, query) as rank "
    "FROM articles, plainto_tsquery('english', $1) query "
    "WHERE search_vector @@ query "
    "ORDER BY rank DESC",
    "python",
)

# Custom weight array: {D_weight, C_weight, B_weight, A_weight}
"ts_rank('{0.1, 0.2, 0.4, 1.0}', search_vector, query) as rank"

# Emphasize title even more:
"ts_rank('{0.05, 0.1, 0.3, 2.0}', search_vector, query) as rank"
```

## Search Index Maintenance

### Automatic with GENERATED ALWAYS

When using `GENERATED ALWAYS AS ... STORED` columns, the index is updated automatically on every INSERT and UPDATE. No maintenance needed.

### Manual Reindexing

If using a manual search_vector column:

```python
# Reindex all rows (after schema change or bulk import)
await db.execute("""
    UPDATE articles SET search_vector =
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'C')
""")

# Reindex specific rows
await db.execute(
    """
    UPDATE articles SET search_vector =
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
    WHERE id = ANY($1)
""",
    updated_ids,
)
```

### GIN Index Maintenance

GIN indexes may become bloated over time. Reindex periodically:

```sql
-- Rebuild the index (locks the table briefly)
REINDEX INDEX idx_articles_search;

-- Or concurrently (no lock, but slower)
REINDEX INDEX CONCURRENTLY idx_articles_search;
```

## Performance Tips

### GIN vs GiST Indexes

| Feature     | GIN                         | GiST                               |
| ----------- | --------------------------- | ---------------------------------- |
| Build time  | Slower                      | Faster                             |
| Query time  | Faster                      | Slower                             |
| Update time | Slower (batch updates help) | Faster                             |
| Size        | Larger                      | Smaller                            |
| Best for    | Read-heavy (search)         | Write-heavy with occasional search |

**Recommendation:** Use GIN for nearly all search use cases. GiST is only better for tables with very frequent writes and rare searches.

```sql
-- GIN (recommended for search)
CREATE INDEX idx_search ON articles USING gin(search_vector);

-- GiST (alternative for write-heavy tables)
CREATE INDEX idx_search ON articles USING gist(search_vector);
```

### Partial Indexes

Index only rows that are searchable (e.g., published articles):

```sql
CREATE INDEX idx_search_published ON articles USING gin(search_vector)
    WHERE status = 'published';
```

Search queries must include the WHERE clause to use the partial index:

```python
results = await db.query(
    "SELECT * FROM articles "
    "WHERE status = 'published' AND search_vector @@ plainto_tsquery('english', $1)",
    q,
)
```

### Avoid ts_headline on Large Result Sets

`ts_headline` re-processes the original text, which is expensive. Apply it only to the final displayed results:

```python
# GOOD: Rank first, then headline only the top results
results = await db.query(
    "WITH ranked AS ("
    "  SELECT id, title, content, ts_rank(search_vector, query) as rank "
    "  FROM articles, plainto_tsquery('english', $1) query "
    "  WHERE search_vector @@ query "
    "  ORDER BY rank DESC LIMIT 20"
    ") "
    "SELECT id, title, rank, "
    "  ts_headline('english', content, plainto_tsquery('english', $1), "
    "    'StartSel=<mark>, StopSel=</mark>, MaxWords=35') as snippet "
    "FROM ranked",
    q,
)

# BAD: Compute headline for every matching row before limiting
"SELECT *, ts_headline(...) as snippet FROM articles WHERE ... ORDER BY rank DESC LIMIT 20"
```

### Pre-Computed tsvector Columns

Always use stored tsvector columns rather than computing them at query time:

```python
# GOOD: Pre-computed column with GIN index
"WHERE search_vector @@ query"

# BAD: Computes tsvector for every row at query time (full table scan)
"WHERE to_tsvector('english', title || ' ' || content) @@ query"
```

### Index-Only Scans

For count-only queries, PostgreSQL can use index-only scans on GIN indexes:

```python
count = await db.query_val(
    "SELECT COUNT(*) FROM articles WHERE search_vector @@ plainto_tsquery('english', $1)",
    q,
)
```

### Benchmarks

With a GIN index on a tsvector column:

- Simple search on 1M rows: < 1ms
- Search with ranking on 1M rows: 2-5ms
- Search with ranking + headline on 1M rows: 10-50ms (headline is the bottleneck)
- Trigram similarity on 1M rows: 5-20ms (depends on threshold)
