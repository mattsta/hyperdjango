# Custom File Storage

HyperDjango provides a pluggable file storage system for uploading, retrieving, and managing files. The built-in backends cover local filesystem and in-memory (testing) storage. You can write custom backends for S3, GCS, or any other storage system by implementing the `Storage` interface.

## Built-in Backends

### FileSystemStorage

The default backend. Stores files on the local filesystem with atomic writes (write to temp file, then `os.replace`).

```python
from hyperdjango.storage import FileSystemStorage

storage = FileSystemStorage(location="/var/uploads", base_url="/media/")

# Save a file (returns final name, may append _1, _2 to avoid conflicts)
name = await storage.save("photos/avatar.jpg", image_bytes)

# Get the URL
url = storage.url(name)  # "/media/photos/avatar.jpg"

# Read file contents
data = await storage.open(name)  # bytes

# Check existence
exists = await storage.exists(name)  # True

# Get file size
size = await storage.size(name)  # int (bytes)

# List directory contents
dirs, files = await storage.listdir("photos/")

# Delete
await storage.delete(name)
```

Path traversal is prevented -- `..` is stripped and leading `/` is removed from all file names.

### MemoryStorage

An in-memory backend for tests. Thread-safe. Files are stored in a dict and lost when the process exits.

```python
from hyperdjango.storage import MemoryStorage

storage = MemoryStorage(base_url="/media/")

name = await storage.save("test.txt", b"hello")
data = await storage.open(name)  # b"hello"
exists = await storage.exists(name)  # True
await storage.delete(name)
```

### Global Storage

Use `get_storage()` to access the app-configured storage backend:

```python
from hyperdjango.storage import get_storage

storage = get_storage()
name = await storage.save("uploads/report.pdf", pdf_bytes)
```

## The Storage Interface

Every storage backend must subclass `Storage` and implement these async methods:

| Method               | Signature                                                 | Description                                                                    |
| -------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `save`               | `async save(name: str, content: bytes) -> str`            | Save content. Return the final file name (may be modified to avoid conflicts). |
| `open`               | `async open(name: str) -> bytes`                          | Read and return file contents. Raise `FileNotFoundError` if missing.           |
| `delete`             | `async delete(name: str)`                                 | Delete a file.                                                                 |
| `exists`             | `async exists(name: str) -> bool`                         | Return `True` if the file exists.                                              |
| `url`                | `url(name: str) -> str`                                   | Return the public URL for a file. Synchronous.                                 |
| `size`               | `async size(name: str) -> int`                            | Return file size in bytes.                                                     |
| `listdir`            | `async listdir(path: str) -> tuple[list[str], list[str]]` | Return `(directories, files)` at the given path.                               |
| `get_available_name` | `get_available_name(name: str) -> str`                    | Return a filename that does not conflict with existing files. Synchronous.     |

## Writing a Custom Backend

Here is a pattern for an S3-compatible storage backend:

```python
from dataclasses import dataclass, field

import boto3

from hyperdjango.storage import Storage


@dataclass
class S3Storage(Storage):
    """S3-compatible file storage backend."""

    bucket_name: str = "my-bucket"
    region: str = "us-east-1"
    base_url: str = ""
    prefix: str = ""
    _client: object = field(init=False, repr=False, default=None)

    def __post_init__(self):
        self._client = boto3.client("s3", region_name=self.region)
        if not self.base_url:
            self.base_url = (
                f"https://{self.bucket_name}.s3.{self.region}.amazonaws.com/"
            )

    def _key(self, name: str) -> str:
        """Build the full S3 object key."""
        name = name.replace("..", "").lstrip("/")
        if self.prefix:
            return f"{self.prefix.rstrip('/')}/{name}"
        return name

    async def save(self, name: str, content: bytes) -> str:
        key = self._key(name)
        self._client.put_object(Bucket=self.bucket_name, Key=key, Body=content)
        return name

    async def open(self, name: str) -> bytes:
        key = self._key(name)
        try:
            response = self._client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read()
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"File not found: {name}")

    async def delete(self, name: str):
        key = self._key(name)
        self._client.delete_object(Bucket=self.bucket_name, Key=key)

    async def exists(self, name: str) -> bool:
        key = self._key(name)
        try:
            self._client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception:
            return False

    def url(self, name: str) -> str:
        return f"{self.base_url}{self._key(name)}"

    async def size(self, name: str) -> int:
        key = self._key(name)
        response = self._client.head_object(Bucket=self.bucket_name, Key=key)
        return response["ContentLength"]
```

## Integration with FileField / ImageField

Models use `FileField` and `ImageField` to manage file uploads. The storage backend is used automatically:

```python
from hyperdjango.models import Model, Field


class Document(Model):
    title: str = Field()
    file: str = Field(file_field=True, upload_to="documents/")


class Photo(Model):
    caption: str = Field()
    image: str = Field(image_field=True, upload_to="photos/", max_size=5_000_000)
```

When saving a model with a file field, the file is stored via the configured storage backend and the field value is set to the storage path. The `upload_to` prefix is prepended to the filename.

## Selecting a Backend

Set the storage backend at the app level:

```python
from hyperdjango import HyperApp
from hyperdjango.storage import FileSystemStorage, configure_storage

app = HyperApp("myapp")

# Use filesystem storage (default)
configure_storage(FileSystemStorage(location="./uploads", base_url="/media/"))

# Or use S3 in production
# configure_storage(S3Storage(bucket_name="prod-uploads", region="us-west-2"))

# Or use MemoryStorage in tests
# configure_storage(MemoryStorage())
```

## See Also

- [Files](files.md) -- FileField and ImageField on models
- [Static Files](static-files.md) -- serving CSS, JS, and images with caching
- [Testing](testing.md) -- MemoryStorage for test isolation
