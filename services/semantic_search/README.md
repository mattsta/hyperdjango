# Semantic Search

pgvector-powered nearest-neighbor search with HNSW cosine indexing and OpenAI-compatible embeddings API.

## Quick Start

```bash
export EMBEDDINGS_API_KEY=sk-...
uv run hyper setup --app services.semantic_search.app:app --seed services.semantic_search.seed:run
uv run hyper run --app services.semantic_search.app:app --port 8200
```

## Features

- VectorField with HNSW cosine index (auto-created by `hyper setup`)
- Real dense embeddings from any OpenAI-compatible API (OpenAI, Ollama, vLLM, Together)
- Nearest-neighbor search via parameterized cosine distance (`<=>` operator)
- Category-filtered vector search
- Similar articles on detail pages via vector distance
- Query timing display showing sub-millisecond HNSW performance
- Article CRUD with automatic embedding on submission
- JSON API accepting text queries or raw vectors
- Server-rendered HTML with search box, results, and article pages
- Session auth for article submission

## Platform Features Demonstrated

- **VectorField** with HNSW index and cosine distance ops
- **pgvector integration** with parameterized `embedding <=> $1::vector` queries
- **Model** with custom field types (VectorField, Enum)
- **Template rendering** with Zig template engine
- **SessionAuth** for authenticated article submission
- **CSRFMiddleware** with exempt API paths
- **Readiness probe** verifying pgvector extension installation

## Configuration

```bash
EMBEDDINGS_API_URL=https://api.openai.com/v1     # Any OpenAI-compatible endpoint
EMBEDDINGS_API_KEY=sk-...                          # Required
EMBEDDINGS_MODEL=text-embedding-3-small            # Model name
VECTOR_DIM=1536                                    # Must match model output
DATABASE_URL=postgres://localhost/hyperdjango_test
```

Supported providers: OpenAI, Ollama (`http://localhost:11434/v1`), vLLM, Together AI.

## Pages and API

```
GET  /                  Home page with search box and recent articles
GET  /search?q=...      Nearest-neighbor search with optional category filter
GET  /article/{id}      Article detail with 5 similar articles
GET  /submit            Article submission form (auth required)
POST /submit            Submit article (auto-embeds title + body)
POST /api/search        JSON search API (text or raw vector input)
POST /api/embed         Get embedding vector for text (auth required)
GET  /stats             System stats (article count, model config, index info)
GET  /admin/            HyperAdmin dashboard
GET  /admin/login/      Admin login
```

## HyperAdmin Panel

Admin panel at `/admin/` with:

- User model: search by username
- Article model: search by title/body, filter by category, ordered by created_at
- Embedding vector field excluded from admin forms (managed by the embeddings API)

## Project Structure

```
semantic_search/
    app.py          Models (VectorField), embeddings client, search routes, admin
    seed.py         Sample articles with pre-computed embeddings
    setup.py        pgvector extension setup
    templates/      HTML templates (index, detail, submit, login, register)
```
