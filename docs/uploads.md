# File Uploads

HyperDjango supports three upload modes that the framework selects automatically
based on file size. All modes use the same API — handlers don't need to know
which mode is active.

## Three Upload Modes

### Mode 1: Memory (default for small files)

Files smaller than `FILE_UPLOAD_MAX_MEMORY_SIZE` (default 2.5 MB) are buffered
in memory. This is the fastest path — zero disk I/O.

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()
    photo = files["photo"]
    photo.data  # bytes — in memory
    photo.size  # int
    photo.in_memory  # True
```

### Mode 2: Disk Spill (for medium/large files)

Files larger than the memory threshold are written to a temporary file during
multipart parsing. `.data` reads from disk lazily. `.path` gives the temp file
path for zero-copy operations.

```python
@app.post("/upload-large")
async def upload_large(request):
    files = await request.files()
    video = files["video"]
    video.in_memory  # False
    video.path  # "/tmp/hyper_upload_abc123" — temp file
    video.size  # int — known without reading

    # Stream from disk in chunks (bounded memory)
    async for chunk in video.chunks():
        await process(chunk)
```

### Mode 3: Pass-Through Streaming (for proxying to S3/CDN)

For multi-GB uploads proxied to external services, the body is never buffered
at all. Bytes flow directly from the TCP socket to your handler in chunks.

```python
@app.post("/proxy-to-s3")
async def proxy_to_s3(request):
    async for chunk in request.stream():
        await s3_client.upload_part(chunk)
```

This mode activates automatically when `Content-Length` exceeds `MAX_BODY_SIZE`
(default 10 MB). The Zig server reads chunks from the TCP socket on demand —
bounded memory regardless of upload size.

## UploadedFile API

Instances are returned by `request.files()` — not user-instantiated.

```python
class UploadedFile:
    filename: str  # Original filename from Content-Disposition
    content_type: str  # MIME type from Content-Type header

    @property
    def data(self) -> bytes:
        """Full file content. Reads from disk if spilled."""

    @property
    def size(self) -> int:
        """File size in bytes."""

    @property
    def path(self) -> str | None:
        """Temp file path (disk mode), or None."""

    @property
    def in_memory(self) -> bool:
        """True if file data is in RAM."""

    async def chunks(self, chunk_size: int = 0) -> AsyncIterator[bytes]:
        """Stream file content in chunks. Works for all modes."""
```

## request.stream()

For pass-through proxying without multipart parsing:

```python
async def stream(self, chunk_size: int = 0) -> AsyncIterator[bytes]:
    """Stream raw body bytes directly from the TCP socket."""
```

When the body exceeds `MAX_BODY_SIZE`, chunks are pulled from the Zig server's
TCP socket reader via FFI — true streaming with bounded memory. For small bodies
or TestClient, falls back to yielding the buffered body in chunks.

## Settings

| Setting                       | Type | Default | Description                                                            |
| ----------------------------- | ---- | ------- | ---------------------------------------------------------------------- |
| `MAX_BODY_SIZE`               | int  | 10 MB   | Threshold: bodies smaller are buffered in memory, larger use streaming |
| `FILE_UPLOAD_MAX_MEMORY_SIZE` | int  | 2.5 MB  | Per-file threshold: smaller in memory, larger spill to disk            |
| `FILE_UPLOAD_MAX_SIZE`        | int  | 0       | Per-file max size during parsing (0 = unlimited)                       |
| `FILE_UPLOAD_TEMP_DIR`        | str  | `""`    | Temp directory for spilled files (empty = system default)              |
| `STREAM_BODY_CHUNK_SIZE`      | int  | 256 KB  | Chunk size for `request.stream()` and `UploadedFile.chunks()`          |

## Architecture

```
Small body (< MAX_BODY_SIZE):
  Zig reads full body → PyBytes → request.body → parse multipart → UploadedFile(memory)

Large body (>= MAX_BODY_SIZE):
  Zig stores socket + Content-Length in thread-local
  Python handler calls request.stream() → Zig reads chunk from socket → yields to Python
  OR: multipart parts with files > FILE_UPLOAD_MAX_MEMORY_SIZE spill to temp files
```

The streaming path uses the same Zig worker thread for both the socket read and
the Python handler — no cross-thread coordination, no GIL (Python 3.14t
free-threaded).
