# HyperAI - Multi-Conversation AI Chat Service

A complete AI chat application built with HyperDjango, demonstrating real-time SSE streaming, API key management, tiered rate limiting, and an OpenAI-compatible REST endpoint.

## Features

- **SSE Streaming**: Real-time token-by-token response streaming via Server-Sent Events
- **Multi-Conversation**: Create, switch between, and delete conversations
- **OpenAI-Compatible API**: Drop-in replacement endpoint at `/api/v1/chat/completions`
- **API Key Management**: Generate, list, and revoke API keys with SHA-256 hashing
- **Tiered Rate Limiting**: Free (20/min), Pro (100/min), Enterprise (1000/min)
- **Usage Tracking**: Per-request token counting and cost logging
- **Session Auth**: HMAC-signed cookie sessions with argon2id password hashing
- **Dark Theme UI**: Modern chat interface with HTMX integration

## Setup

```bash
# Create the database
createdb hyperai

# Run the application
cd services/hyperai
uv run hyper run app.py
```

Visit http://localhost:8000 to access the web interface.

## API Documentation

### Authentication

All API requests require a Bearer token:

```
Authorization: Bearer sk-hyper-<your-key>
```

Generate API keys from the Account page in the web interface.

### Chat Completions (Streaming)

```bash
curl -N http://localhost:8000/api/v1/chat/completions \
  -H "Authorization: Bearer sk-hyper-YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hyper-4",
    "messages": [{"role": "user", "content": "Explain SSE streaming"}],
    "stream": true
  }'
```

Response (SSE stream):

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"hyper-4","choices":[{"index":0,"delta":{"content":"That's "},"finish_reason":null}]}
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"hyper-4","choices":[{"index":0,"delta":{"content":"a "},"finish_reason":null}]}
...
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","model":"hyper-4","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

### Chat Completions (Non-Streaming)

```bash
curl http://localhost:8000/api/v1/chat/completions \
  -H "Authorization: Bearer sk-hyper-YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hyper-4",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

### Using with the OpenAI Python Client

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-hyper-YOUR_KEY",
    base_url="http://localhost:8000/api/v1",
)

stream = client.chat.completions.create(
    model="hyper-4",
    messages=[{"role": "user", "content": "What is HyperDjango?"}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## Framework Features Demonstrated

- **HyperApp**: Route decorators, middleware stack, template rendering
- **Models**: Field definitions, QuerySet API (filter, order_by, count, limit)
- **Response.sse()**: Server-Sent Events streaming with async generators
- **SessionAuth**: Login/logout with HMAC-signed cookies
- **Middleware**: Timing, Security Headers, CORS, CSRF (with exemptions), Rate Limiting
- **Templates**: Jinja2 with extends/include, partials, conditional rendering
- **require_auth**: Decorator with login_url redirect for protected routes
- **HTTPException**: Proper error handling with status codes

## HyperAdmin Panel

Admin panel at `/admin/` with all 5 models:

- User (search by username/email, filter by tier)
- Conversation (search by title)
- Message (filter by role)
- APIKey (search by name/prefix, filter by is_active)
- UsageLog (filter by model_name)

## Architecture

```
app.py          — Routes, middleware, AI simulation, API endpoints, admin
models.py       — User, Conversation, Message, APIKey, UsageLog
templates/      — Jinja2 templates with base layout and partials
static/         — CSS for dark sidebar + light chat UI
```

AI responses are simulated (no real LLM calls) to keep the service self-contained. The SSE streaming pattern is identical to what you would use with a real AI API.
