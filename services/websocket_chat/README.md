# WebSocket Chat

Real-time chat application using the native Zig RFC 6455 WebSocket server with rooms, presence tracking, and channel pub/sub.

## Quick Start

```bash
createdb chat
uv run hyper setup --app services.websocket_chat.app:app --seed services.websocket_chat.seed:run
uv run hyper run --app services.websocket_chat.app:app
```

## Features

- Native Zig HTTP server with RFC 6455 WebSocket upgrade (SIMD XOR unmasking)
- Room system with create, join, leave, and member listing
- Real-time presence tracking (online members list)
- Typing indicators broadcast to room members
- Message persistence to PostgreSQL
- Message history loaded on room join
- Channel pub/sub via InMemoryChannelLayer
- WebSocket rate limiting (token bucket + sliding window)
- ConnectionManager for WebSocket lifecycle tracking
- Session auth on both HTTP and WebSocket connections
- CSRF protection (double-submit cookie pattern)
- htmx for interactive room creation (no page reloads)
- Bidirectional async message loop (concurrent read/write)

## Platform Features Demonstrated

- **@app.websocket** route decorator for WebSocket endpoints
- **InMemoryChannelLayer** for pub/sub messaging between connections
- **Room** with RoomConfig (max_members, history_size, rate_limit)
- **ConnectionManager** for tracking active WebSocket connections
- **WebSocketRateLimiter** with token bucket + sliding window
- **SessionAuth** manual verification on WebSocket via cookie parsing
- **SecurityHeadersMiddleware** and **CSRFMiddleware**
- **Template rendering** with Zig template engine
- **Model** definitions with foreign keys and validation

## WebSocket Protocol

Connect to `ws://localhost:8000/ws/chat?room_id={id}` with a valid session cookie.

Messages sent to server:

```json
{"type": "message", "content": "Hello!"}
{"type": "typing", "typing": true}
```

Messages received from server:

```json
{"type": "history", "messages": [...]}
{"type": "presence", "members": [{"user_id": "1", "display_name": "alice"}]}
{"type": "message", "user_id": "1", "content": "Hello!"}
{"type": "typing", "user_id": "1", "typing": true}
{"type": "rate_limited", "message": "Too many messages"}
{"type": "error", "message": "..."}
```

## Pages

```
GET  /                  Room list (auth required)
POST /rooms/create      Create room (htmx partial response)
GET  /rooms/{id}        Chat room page with WebSocket client
GET  /login             Login page
GET  /register          Registration page
GET  /logout            Logout
GET  /api/rooms/{id}/history    Room message history (JSON)
```

## HyperAdmin Panel

Admin panel at `/admin/` with all 3 models:

- User (search by username)
- ChatRoom (search by name)
- ChatMessage (search by username/content, filter by room)

## Project Structure

```
websocket_chat/
    app.py          Models, WebSocket handler, room management, auth, admin
    seed.py         Sample users and rooms
    templates/      HTML templates (index, room, login, register)
    static/         CSS and JavaScript (WebSocket client)
```
