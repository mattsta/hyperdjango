# Files & Uploads

File handling, upload processing, and storage backends. HyperDjango provides a complete file management system with model-level file fields, pluggable storage backends, a native Zig SIMD multipart parser, and security protections built in.

## Quick Start

```python
from hyperdjango.storage import FileSystemStorage
from hyperdjango.models import Model, Field, FileField, ImageField, save_uploaded_file

storage = FileSystemStorage(location="/var/uploads", base_url="/media/")


class Product(Model):
    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    document: str = FileField(upload_to="documents/")
    photo: str = ImageField(upload_to="products/photos/")


@app.post("/products/{id}/photo")
async def upload_photo(request, id: int):
    product = await Product.objects.get(id=id)
    files = await request.files()
    path = await save_uploaded_file(
        product, "photo", files["photo"], "photo.jpg", storage
    )
    await product.save()
    return {"photo_url": storage.url(path)}
```

## File Uploads

### Receiving Uploads

File uploads are available via `request.files()`, which returns a dict mapping field names to byte content.

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()
    content = files["document"]  # bytes

    path = await storage.save("uploads/report.pdf", content)
    return {"path": path, "size": len(content)}
```

### Multipart Form Data

The native Zig multipart parser handles `multipart/form-data` at 20.4 GB/s boundary scanning on 100KB bodies. Text fields and file fields are parsed in a single pass using SIMD-accelerated boundary detection.

```python
@app.post("/submit")
async def submit(request):
    form = await request.form_data()  # Text fields (dict[str, str])
    files = await request.files()  # File fields (dict[str, bytes])

    name = form["name"]
    avatar = files["avatar"]  # bytes
    return {"name": name, "avatar_size": len(avatar)}
```

The Zig parser handles:

- RFC 2046 multipart boundary scanning with SIMD `@Vector(16, u8)` byte matching
- Content-Disposition header parsing for field names and filenames
- Streaming large uploads without buffering the entire body in memory
- Proper handling of nested multipart (though rare in practice)

### Accessing Upload Metadata

The `request.files()` dict returns raw bytes. If you need the original filename or content type, use the multipart headers:

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()
    form = await request.form_data()

    file_bytes = files["document"]
    # The original filename is typically in form_data or sent separately
    filename = form.get("filename", "upload.bin")
    path = await storage.save(f"uploads/{filename}", file_bytes)
    return {"path": path}
```

## FileField & ImageField

Declare file fields on models. These are string fields that store a file path relative to the storage root. The field metadata (`upload_to`, `allowed_extensions`, `file_field_type`) is stored directly on the `FieldInfo` dataclass.

### FileField

```python
from hyperdjango.models import Model, Field, FileField


class Document(Model):
    class Meta:
        table = "documents"

    id: int = Field(primary_key=True, auto=True)
    title: str = Field()
    file: str = FileField(upload_to="documents/")
```

**FileField parameters:**

| Parameter    | Type  | Default      | Description                                |
| ------------ | ----- | ------------ | ------------------------------------------ |
| `upload_to`  | `str` | `"uploads/"` | Storage subdirectory for uploaded files    |
| `max_length` | `int` | `500`        | Maximum path length stored in the database |
| `default`    | `str` | `""`         | Default value (empty string = no file)     |

The `FileField()` function creates a `FieldInfo` with `file_field_type="file"` and the specified `upload_to` path (with trailing slash ensured).

### ImageField

```python
from hyperdjango.models import Model, Field, ImageField


class Profile(Model):
    class Meta:
        table = "profiles"

    id: int = Field(primary_key=True, auto=True)
    user_id: int = Field(foreign_key=User)
    avatar: str = ImageField(
        upload_to="avatars/",
        allowed_extensions=(".jpg", ".jpeg", ".png", ".webp"),
    )
```

**ImageField parameters:**

| Parameter            | Type              | Default                                              | Description             |
| -------------------- | ----------------- | ---------------------------------------------------- | ----------------------- |
| `upload_to`          | `str`             | `"images/"`                                          | Storage subdirectory    |
| `max_length`         | `int`             | `500`                                                | Maximum path length     |
| `allowed_extensions` | `tuple[str, ...]` | `(".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")` | Allowed file extensions |
| `default`            | `str`             | `""`                                                 | Default value           |

`ImageField` validates file extensions on upload. If the uploaded file has an extension not in `allowed_extensions`, a `ValueError` is raised. The `ImageField()` function creates a `FieldInfo` with `file_field_type="image"`.

### file_field_type Attribute

Both `FileField` and `ImageField` set the `file_field_type` attribute on the `FieldInfo`:

- `FileField`: `file_field_type = "file"`
- `ImageField`: `file_field_type = "image"`
- Regular fields: `file_field_type = None`

This attribute is used by `save_uploaded_file()` to determine validation rules and by the admin to render appropriate form widgets.

## Storage Backends

### FileSystemStorage

The primary storage backend. Stores files on the local filesystem with atomic writes and path traversal prevention.

```python
from hyperdjango.storage import FileSystemStorage

storage = FileSystemStorage(
    location="/var/uploads",  # Absolute path to storage root
    base_url="/media/",  # URL prefix for serving files
)
```

**Constructor parameters:**

| Parameter  | Type  | Default     | Description                                    |
| ---------- | ----- | ----------- | ---------------------------------------------- |
| `location` | `str` | `"uploads"` | Absolute or relative path to storage directory |
| `base_url` | `str` | `"/media/"` | URL prefix for `url()` method                  |

### FileSystemStorage Full API

#### save(name, content) -> str

Save bytes to a file. Creates parent directories as needed. Returns the final file name (may be modified to avoid conflicts).

```python
path = await storage.save("photos/avatar.jpg", image_bytes)
# Returns: "photos/avatar.jpg"
# Or: "photos/avatar_1.jpg" if the original name was taken
```

**How saving works:**

1. The name is sanitized (remove `..`, strip leading `/`)
2. `get_available_name()` is called to avoid overwriting existing files
3. Parent directories are created with `os.makedirs`
4. Content is written to a temp file (`{path}.tmp.{pid}`)
5. The temp file is atomically renamed to the final path via `os.replace()`
6. If any step fails, the temp file is cleaned up

#### open(name) -> bytes

Read and return the contents of a file.

```python
content = await storage.open("photos/avatar.jpg")
# Returns: bytes
```

Raises `FileNotFoundError` if the file does not exist.

#### delete(name)

Delete a file. No error is raised if the file does not exist.

```python
await storage.delete("photos/avatar.jpg")
```

#### exists(name) -> bool

Check if a file exists.

```python
if await storage.exists("photos/avatar.jpg"):
    content = await storage.open("photos/avatar.jpg")
```

#### listdir(path) -> tuple[list[str], list[str]]

List directories and files at the given path. Returns `(directories, files)` with both lists sorted alphabetically.

```python
dirs, files = await storage.listdir("photos/")
# dirs: ["thumbnails", "originals"]
# files: ["avatar.jpg", "banner.png"]

# List root
dirs, files = await storage.listdir("")
```

Returns `([], [])` if the path does not exist.

#### url(name) -> str

Return the URL for serving a file. Combines `base_url` with the file name.

```python
url = storage.url("photos/avatar.jpg")
# "/media/photos/avatar.jpg"
```

#### size(name) -> int

Return the file size in bytes.

```python
size = await storage.size("photos/avatar.jpg")
# 245760
```

Raises `FileNotFoundError` if the file does not exist.

#### path(name) -> str (via \_path)

Get the full filesystem path for a file name. The name is sanitized to prevent directory traversal.

```python
full_path = storage._path("photos/avatar.jpg")
# "/var/uploads/photos/avatar.jpg"
```

#### get_available_name(name) -> str

Return a filename that does not conflict with existing files. If the name is taken, appends `_1`, `_2`, etc. before the extension.

```python
# If "avatar.jpg" exists:
name = storage.get_available_name("avatar.jpg")
# "avatar_1.jpg"

# If "avatar_1.jpg" also exists:
name = storage.get_available_name("avatar.jpg")
# "avatar_2.jpg"
```

Raises `RuntimeError` if more than 10,000 candidates are tried (prevents infinite loops).

### MemoryStorage

In-memory storage for testing. Thread-safe via a `threading.Lock`.

```python
from hyperdjango.storage import MemoryStorage

storage = MemoryStorage(base_url="/media/")

# Full API is identical to FileSystemStorage
path = await storage.save("test.txt", b"hello")
content = await storage.open(path)
assert content == b"hello"

exists = await storage.exists(path)
assert exists is True

size = await storage.size(path)
assert size == 5

dirs, files = await storage.listdir("")
assert files == ["test.txt"]

await storage.delete(path)
assert await storage.exists(path) is False
```

**Additional methods:**

#### clear()

Remove all stored files. Useful in test setup/teardown.

```python
storage.clear()
assert await storage.exists("test.txt") is False
```

### Global Storage Instance

Set a global default storage backend for the application:

```python
from hyperdjango.storage import set_storage, get_storage

# At app startup
set_storage(FileSystemStorage(location="/var/uploads", base_url="/media/"))

# In any module
storage = get_storage()
path = await storage.save("file.txt", b"content")
```

`get_storage()` raises `RuntimeError` if no storage has been configured.

## File Lifecycle Helpers

### save_uploaded_file()

Save an uploaded file to storage and update the model instance's field value.

```python
from hyperdjango.models import save_uploaded_file

path = await save_uploaded_file(
    model_instance,  # The model instance (e.g., product)
    field_name,  # The field name (e.g., "photo")
    content,  # File bytes
    filename,  # Original filename (e.g., "photo.jpg")
    storage,  # Storage backend instance
)
```

**What it does:**

1. Looks up the `FieldInfo` for the given field name on the model
2. Resolves the `upload_to` path from the field metadata
3. If the field is an `ImageField`, validates the file extension against `allowed_extensions`
4. Constructs the storage path: `{upload_to}/{filename}`
5. Saves the file via the storage backend
6. Updates the model instance's field value with the stored path
7. Returns the stored path

**Extension validation for ImageField:**

```python
class Product(Model):
    photo: str = ImageField(
        upload_to="products/",
        allowed_extensions=(".jpg", ".jpeg", ".png"),
    )


# This succeeds:
await save_uploaded_file(product, "photo", bytes, "pic.jpg", storage)

# This raises ValueError:
await save_uploaded_file(product, "photo", bytes, "pic.bmp", storage)
# ValueError: Extension '.bmp' not allowed. Allowed: ('.jpg', '.jpeg', '.png')
```

### delete_uploaded_file()

Delete an uploaded file from storage and clear the model instance's field value.

```python
from hyperdjango.models import delete_uploaded_file

await delete_uploaded_file(
    model_instance,  # The model instance
    field_name,  # The field name (e.g., "photo")
    storage,  # Storage backend instance
)
```

**What it does:**

1. Reads the current file path from the model instance's field
2. Deletes the file from storage
3. Sets the model field to an empty string
4. You must call `await model_instance.save()` separately to persist the change

### Full Upload/Delete Example

```python
@app.post("/products/{id}/photo")
async def upload_photo(request, id: int):
    product = await get_object_or_404(Product, id=id)
    files = await request.files()
    photo_bytes = files["photo"]
    filename = "photo.jpg"

    # Delete old photo if exists
    if product.photo:
        await delete_uploaded_file(product, "photo", storage)

    # Save new photo
    path = await save_uploaded_file(product, "photo", photo_bytes, filename, storage)
    await product.save()
    return {"photo_path": path, "photo_url": storage.url(path)}


@app.delete("/products/{id}/photo")
async def delete_photo(request, id: int):
    product = await get_object_or_404(Product, id=id)
    if not product.photo:
        raise HTTPException(404, "No photo to delete")
    await delete_uploaded_file(product, "photo", storage)
    await product.save()
    return {"deleted": True}
```

## Multipart Upload Handling (Zig SIMD Parser)

HyperDjango uses a native Zig multipart parser for processing `multipart/form-data` uploads. The parser uses SIMD vector operations for boundary scanning, achieving 20.4 GB/s throughput on 100KB bodies.

### How It Works

The Zig multipart parser (`zig/src/multipart.zig`) processes the raw request body:

1. Extracts the boundary string from the `Content-Type` header
2. Scans the body for boundary markers using `@Vector(16, u8)` SIMD operations
3. Parses `Content-Disposition` headers for each part to extract field names and filenames
4. Separates text fields from file fields based on the presence of a filename
5. Returns structured data to Python: text fields as strings, file fields as bytes

### Performance

| Metric            | Value                                 |
| ----------------- | ------------------------------------- |
| Boundary scanning | 20.4 GB/s on 100KB bodies             |
| SIMD width        | 16 bytes per cycle                    |
| Parser type       | Single-pass, zero-copy where possible |

### Large File Uploads

For very large files, consider these approaches:

```python
@app.post("/upload-large")
async def upload_large(request):
    files = await request.files()
    content = files["file"]

    # Check size before saving
    MAX_SIZE = 100 * 1024 * 1024  # 100MB
    if len(content) > MAX_SIZE:
        raise HTTPException(413, f"File too large (max {MAX_SIZE // 1024 // 1024}MB)")

    path = await storage.save("large-files/upload.dat", content)
    return {"path": path, "size": len(content)}
```

## File Upload Security

HyperDjango provides multiple layers of file upload security.

### Path Traversal Prevention

Storage backends reject paths containing `..` or absolute paths. The `_path()` method strips these:

```python
# All of these are sanitized:
storage._path("../../etc/passwd")  # -> "/var/uploads/etc/passwd"
storage._path("/etc/passwd")  # -> "/var/uploads/etc/passwd"
storage._path("../../../root/.ssh/id_rsa")  # -> "/var/uploads/root/.ssh/id_rsa"
```

The `..` sequences are removed and leading `/` is stripped before joining with the storage `location`.

### Extension Validation

`ImageField` rejects files with extensions not in its `allowed_extensions` list:

```python
class Upload(Model):
    avatar: str = ImageField(
        upload_to="avatars/",
        allowed_extensions=(".jpg", ".jpeg", ".png", ".gif", ".webp"),
    )


# Raises ValueError for .exe, .php, .sh, etc.
```

For `FileField`, there is no built-in extension validation. Add your own if needed:

```python
ALLOWED_DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}


@app.post("/upload-document")
async def upload_doc(request):
    files = await request.files()
    filename = form.get("filename", "upload.bin")

    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_DOC_EXTENSIONS:
        raise HTTPException(400, f"Extension {ext} not allowed")

    content = files["document"]
    path = await storage.save(f"documents/{filename}", content)
    return {"path": path}
```

### Size Limits

Enforce file size limits in your upload handlers:

```python
@app.post("/upload")
async def upload(request):
    files = await request.files()
    content = files["file"]

    MAX_SIZE = 10 * 1024 * 1024  # 10MB
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File too large (max 10MB)")

    path = await storage.save("uploads/file.dat", content)
    return {"path": path}
```

For application-wide limits, use middleware:

```python
class MaxUploadSizeMiddleware:
    def __init__(self, max_size: int = 10 * 1024 * 1024):
        self.max_size = max_size

    async def __call__(self, request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_size:
            raise HTTPException(
                413, f"Request body too large (max {self.max_size} bytes)"
            )
        return await call_next(request)


app.use(MaxUploadSizeMiddleware(max_size=50 * 1024 * 1024))
```

### Atomic Writes

`FileSystemStorage` writes to a temp file then atomically renames it. This prevents:

- Partial file reads (another request reading a half-written file)
- Corrupt files on crash (temp file is cleaned up, original is untouched)
- Race conditions between concurrent uploads to the same path

The temp file uses the process ID for uniqueness: `{path}.tmp.{os.getpid()}`

### Content Type Validation

For additional security, validate the actual file content (not just the extension):

```python
import imghdr


@app.post("/upload-image")
async def upload_image(request):
    files = await request.files()
    content = files["image"]

    # Check actual file type via magic bytes
    file_type = imghdr.what(None, h=content)
    if file_type not in ("jpeg", "png", "gif", "webp"):
        raise HTTPException(400, "Invalid image file")

    path = await storage.save("images/upload.jpg", content)
    return {"path": path}
```

## Testing File Uploads

### With TestClient

```python
from hyperdjango.testing import TestClient

client = TestClient(app)

# Upload a file via multipart form
response = client.post(
    "/upload",
    files={
        "document": ("report.pdf", b"PDF content here", "application/pdf"),
    },
)
assert response.status == 200
assert "path" in response.json()
```

The `files` parameter accepts a dict where each value is a tuple of `(filename, content, content_type)`.

### Multiple File Upload

```python
response = client.post(
    "/upload-multiple",
    files={
        "photo": ("photo.jpg", b"JPEG data", "image/jpeg"),
        "document": ("report.pdf", b"PDF data", "application/pdf"),
    },
)
```

### Mixed Form Data and Files

```python
response = client.post(
    "/submit",
    data={
        "title": "My Product",
        "description": "A great product",
    },
    files={
        "photo": ("photo.jpg", b"JPEG data", "image/jpeg"),
    },
)
```

### Using MemoryStorage in Tests

```python
from hyperdjango.storage import MemoryStorage, set_storage

storage = MemoryStorage()
set_storage(storage)


async def test_upload():
    client = TestClient(app)
    response = client.post(
        "/upload",
        files={
            "file": ("test.txt", b"hello world", "text/plain"),
        },
    )
    assert response.status == 200

    # Verify file was stored
    path = response.json()["path"]
    content = await storage.open(path)
    assert content == b"hello world"

    # Clean up
    storage.clear()
```

### Testing File Deletion

```python
async def test_delete_photo():
    # Setup: create product with photo
    product = await Product.objects.create(name="Widget")
    await save_uploaded_file(product, "photo", b"JPEG data", "widget.jpg", storage)
    await product.save()

    # Delete photo
    client = TestClient(app)
    response = client.delete(f"/products/{product.id}/photo")
    assert response.status == 200

    # Verify file was deleted
    assert not await storage.exists(product.photo)

    # Verify model field was cleared
    product = await Product.objects.get(id=product.id)
    assert product.photo == ""
```

## Custom Storage Backends

Create custom storage backends by subclassing `Storage`:

```python
from hyperdjango.storage import Storage


class S3Storage(Storage):
    """Store files in Amazon S3."""

    def __init__(self, bucket: str, region: str = "us-east-1", base_url: str = ""):
        self.bucket = bucket
        self.region = region
        self.base_url = base_url or f"https://{bucket}.s3.{region}.amazonaws.com/"

    async def save(self, name: str, content: bytes) -> str:
        name = name.replace("..", "").lstrip("/")
        # Upload to S3 via boto3 or httpx
        await self._upload(name, content)
        return name

    async def open(self, name: str) -> bytes:
        return await self._download(name)

    async def delete(self, name: str):
        await self._delete_object(name)

    async def exists(self, name: str) -> bool:
        return await self._head_object(name)

    def url(self, name: str) -> str:
        return f"{self.base_url}{name}"

    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        objects = await self._list_objects(prefix=path)
        # Parse S3 response into dirs and files
        ...

    async def size(self, name: str) -> int:
        metadata = await self._head_object(name)
        return metadata["ContentLength"]

    def get_available_name(self, name: str) -> str:
        # S3 allows overwriting, so just return the name
        return name
```

### Storage Backend Interface

All storage backends must implement these methods:

| Method               | Signature                       | Description                       |
| -------------------- | ------------------------------- | --------------------------------- |
| `save`               | `async (name, content) -> str`  | Save bytes, return final name     |
| `open`               | `async (name) -> bytes`         | Read file contents                |
| `delete`             | `async (name) -> None`          | Delete file (no error if missing) |
| `exists`             | `async (name) -> bool`          | Check if file exists              |
| `url`                | `(name) -> str`                 | Return URL for serving the file   |
| `listdir`            | `async (path) -> (dirs, files)` | List directory contents           |
| `size`               | `async (name) -> int`           | Return file size in bytes         |
| `get_available_name` | `(name) -> str`                 | Return non-conflicting name       |

## Serving Uploaded Files

### Development (Static File Middleware)

In development, serve uploads via the static files middleware:

```python
from hyperdjango.staticfiles import StaticFilesMiddleware

app.use(
    StaticFilesMiddleware(
        prefix="/media/",
        directory="/var/uploads",
    )
)
```

### Production

In production, serve files via your reverse proxy (nginx):

```nginx
location /media/ {
    alias /var/uploads/;
    expires 30d;
    add_header Cache-Control "public, immutable";
}
```

### Generating Download Responses

```python
from hyperdjango.response import Response


@app.get("/download/{filename}")
async def download(request, filename: str):
    content = await storage.open(filename)
    return Response.attachment(content, filename=filename)
```
