"""
File storage abstraction for HyperApp.

Pluggable backends for file upload, retrieval, and management.
Default: FileSystemStorage. Optional: S3Storage (requires boto3).

Usage:
    from hyperdjango.storage import FileSystemStorage, get_storage

    # Default filesystem storage
    storage = FileSystemStorage(location="/var/uploads", base_url="/media/")

    # Save a file
    name = await storage.save("photos/avatar.jpg", content_bytes)
    # Returns: "photos/avatar.jpg" (or "photos/avatar_1.jpg" if exists)

    # Get URL
    url = storage.url(name)  # "/media/photos/avatar.jpg"

    # Read file
    data = await storage.open(name)  # bytes

    # Check existence
    exists = await storage.exists(name)  # True

    # Delete
    await storage.delete(name)

    # List files
    dirs, files = await storage.listdir("photos/")

    # Global storage (set by app config)
    storage = get_storage()
"""

import asyncio
import errno
import os
import shutil
import threading
import uuid
from pathlib import Path

from hyperdjango.conf import get_setting


class SuspiciousFileOperation(Exception):
    """Raised when a file name would resolve outside the storage root.

    Signals an attempted path-traversal (``..`` segments, absolute paths, or
    symlink escapes) so callers can reject the operation rather than silently
    read/write an unintended location.
    """


class Storage:
    """Abstract base class for file storage backends."""

    async def save(self, name: str, content: bytes) -> str:
        """Save content to a file. Returns the final file name (may be modified to avoid conflicts)."""
        raise NotImplementedError("Subclass must implement this method")

    async def open(self, name: str) -> bytes:
        """Read and return the contents of a file."""
        raise NotImplementedError("Subclass must implement this method")

    async def delete(self, name: str):
        """Delete a file."""
        raise NotImplementedError("Subclass must implement this method")

    async def exists(self, name: str) -> bool:
        """Return True if a file exists."""
        raise NotImplementedError("Subclass must implement this method")

    def url(self, name: str) -> str:
        """Return the URL for a file."""
        raise NotImplementedError("Subclass must implement this method")

    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        """List directories and files at the given path.

        Returns (directories, files).
        """
        raise NotImplementedError("Subclass must implement this method")

    async def size(self, name: str) -> int:
        """Return the size of a file in bytes."""
        raise NotImplementedError("Subclass must implement this method")

    def get_available_name(self, name: str) -> str:
        """Return a filename that's available (doesn't already exist).

        Appends _1, _2, etc. if the name is taken.
        """
        raise NotImplementedError("Subclass must implement this method")


class FileSystemStorage(Storage):
    """Store files on the local filesystem.

    Args:
        location: Absolute path to the storage directory.
        base_url: URL prefix for serving files (e.g., "/media/").
    """

    def __init__(self, location: str = "", base_url: str = ""):
        # Use MEDIA_ROOT / MEDIA_URL from settings as defaults
        if not location:
            location = get_setting("MEDIA_ROOT") or "uploads"
        if not base_url:
            base_url = get_setting("MEDIA_URL")
        self.location = str(Path(location).resolve())
        self.base_url = base_url.rstrip("/") + "/"

    def _path(self, name: str) -> str:
        """Resolve a storage-relative name to a real absolute filesystem path.

        Single containment choke point for save/open/delete/exists/listdir/size:
        the resolved real path MUST stay within the (real) storage root. A name
        with ``..`` segments, an absolute path, or a symlink that escapes the
        root is rejected with SuspiciousFileOperation. Legitimate subdirectories
        are preserved.

        Rejecting (rather than the old ``name.replace("..", "")`` scrub) avoids
        both silent traversal and corrupting legitimate names that merely
        contain "..".
        """
        # A NUL byte makes os.path.realpath/lstat raise ValueError ("embedded
        # null character") — an uncaught 500 on a public upload endpoint. Reject
        # it (and it can never be part of a legitimate filename) as a clean
        # SuspiciousFileOperation → 400, same as a traversal attempt.
        if "\x00" in name:
            raise SuspiciousFileOperation(f"Null byte in file name {name!r}")
        # Leading slashes → treat the name as relative to the root, never as a
        # filesystem-absolute path (os.path.join would otherwise discard root).
        clean = name.lstrip("/")
        root_real = os.path.realpath(self.location)
        candidate = os.path.realpath(Path(root_real) / clean)
        if candidate != root_real and not candidate.startswith(root_real + os.sep):
            raise SuspiciousFileOperation(
                f"Detected path traversal attempt in file name {name!r}"
            )
        return candidate

    async def save(self, name: str, content: bytes) -> str:
        """Save content to a file. Creates directories as needed.

        The blocking filesystem work runs in a thread executor so it never
        stalls the event loop (and every other in-flight request on it).
        """
        return await asyncio.to_thread(self._save_sync, name, content)

    def _save_sync(self, name: str, content: bytes) -> str:
        """Synchronous save implementation (runs in a worker thread).

        Validates file size against MAX_UPLOAD_SIZE, extension against
        ALLOWED_UPLOAD_EXTENSIONS, sets permissions from FILE_UPLOAD_PERMISSIONS,
        and uses FILE_UPLOAD_TEMP_DIR for atomic writes.
        """
        # ── Validate upload size ──
        max_upload_size = get_setting("MAX_UPLOAD_SIZE")
        if max_upload_size and len(content) > max_upload_size:
            raise ValueError(
                f"File size {len(content)} bytes exceeds MAX_UPLOAD_SIZE ({max_upload_size} bytes)"
            )

        # ── Validate extension ──
        allowed_extensions = get_setting("ALLOWED_UPLOAD_EXTENSIONS")
        if allowed_extensions:
            ext = Path(name).suffix
            ext_lower = ext.lower()
            # Normalize: allow both ".jpg" and "jpg" in the setting
            normalized = {
                e.lower() if e.startswith(".") else f".{e.lower()}"
                for e in allowed_extensions
            }
            if ext_lower not in normalized:
                raise ValueError(
                    f"File extension {ext_lower!r} not in ALLOWED_UPLOAD_EXTENSIONS: {sorted(normalized)}"
                )

        # Treat the name as root-relative. Containment (traversal rejection)
        # is enforced at the _path() choke point, reached via
        # _reserve_available_path below — a `..`/absolute escape raises
        # SuspiciousFileOperation there rather than being silently scrubbed.
        name = name.lstrip("/")
        # Atomically RESERVE a distinct name — closes the get_available_name
        # TOCTOU so concurrent saves of the same name get DISTINCT names (never
        # both claim the same one and clobber). Creates the parent dir and an
        # empty placeholder at full_path, which the rename below replaces.
        name, full_path = self._reserve_available_path(name)

        # Apply directory permissions to the (now-created) parent directory
        dir_perms = get_setting("FILE_UPLOAD_DIRECTORY_PERMISSIONS")
        parent_dir = Path(full_path).parent
        if dir_perms and parent_dir.is_dir():
            parent_dir.chmod(dir_perms)

        # Write atomically: write to a UNIQUE temp file then rename.
        # The temp name must be unique per-operation: a shared ".upload.{pid}.tmp"
        # would let two concurrent saves in the same process clobber each other's
        # bytes mid-write (corruption). pid + a uuid makes it collision-free
        # across threads/tasks.
        unique = f"{os.getpid()}.{uuid.uuid4().hex}"
        temp_dir = get_setting("FILE_UPLOAD_TEMP_DIR") or None
        if temp_dir:
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            tmp_path = str(Path(temp_dir) / f".upload.{unique}.tmp")
        else:
            tmp_path = full_path + f".tmp.{unique}"
        try:
            with Path(tmp_path).open("wb") as f:
                f.write(content)
            try:
                Path(tmp_path).replace(full_path)
            except OSError as exc:
                # FILE_UPLOAD_TEMP_DIR may live on a different filesystem than
                # the destination, so an atomic rename fails with EXDEV. Re-stage
                # the bytes into a temp file in the destination directory (same
                # filesystem) and rename from there, so the swap into place stays
                # atomic. Clean up the original cross-fs temp afterward.
                if exc.errno != errno.EXDEV:
                    raise
                dest_tmp = full_path + f".tmp.{unique}"
                shutil.copyfile(tmp_path, dest_tmp)
                try:
                    Path(dest_tmp).replace(full_path)
                except Exception:
                    Path(dest_tmp).unlink(missing_ok=True)
                    raise
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            # Clean up the temp file AND the reserved placeholder on error
            # (the placeholder is only replaced on a successful rename above).
            Path(tmp_path).unlink(missing_ok=True)
            Path(full_path).unlink(missing_ok=True)
            raise

        # Apply file permissions from settings
        file_perms = get_setting("FILE_UPLOAD_PERMISSIONS")
        if file_perms:
            Path(full_path).chmod(file_perms)

        return name

    async def open(self, name: str) -> bytes:
        """Read and return file contents (off the event loop)."""
        return await asyncio.to_thread(self._open_sync, name)

    def _open_sync(self, name: str) -> bytes:
        full_path = self._path(name)
        fp = Path(full_path)
        if not fp.exists():
            raise FileNotFoundError(f"File not found: {name}")
        with fp.open("rb") as f:
            return f.read()

    async def delete(self, name: str):
        """Delete a file. No error if file doesn't exist."""

        def _delete() -> None:
            full_path = Path(self._path(name))
            if full_path.exists():
                full_path.unlink()

        await asyncio.to_thread(_delete)

    async def exists(self, name: str) -> bool:
        """Return True if the file exists."""
        return await asyncio.to_thread(lambda: Path(self._path(name)).exists())

    def url(self, name: str) -> str:
        """Return the URL for serving this file."""
        # Validate containment (raises SuspiciousFileOperation on traversal) so a
        # crafted name can never yield a URL pointing outside the storage root.
        self._path(name)
        return f"{self.base_url}{name.lstrip('/')}"

    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        """List directories and files at the given path (off the event loop)."""
        return await asyncio.to_thread(self._listdir_sync, path)

    def _listdir_sync(self, path: str) -> tuple[list[str], list[str]]:
        full_path = self._path(path) if path else self.location
        if not Path(full_path).exists():
            return [], []

        dirs = []
        files = []
        for entry in os.scandir(full_path):
            if entry.is_dir():
                dirs.append(entry.name)
            elif entry.is_file():
                files.append(entry.name)
        dirs.sort()
        files.sort()
        return dirs, files

    async def size(self, name: str) -> int:
        """Return the file size in bytes (off the event loop)."""
        return await asyncio.to_thread(self._size_sync, name)

    def _size_sync(self, name: str) -> int:
        fp = Path(self._path(name))
        if not fp.exists():
            raise FileNotFoundError(f"File not found: {name}")
        return fp.stat().st_size

    def _reserve_available_path(self, name: str) -> tuple[str, str]:
        """Atomically reserve the first free name and return (name, full_path).

        Creates the parent directory and claims the name by creating an empty
        placeholder with O_CREAT|O_EXCL, iterating name, name_1, name_2, … until
        one is won. This is race-free: two concurrent saves of the same name can
        never both win the same candidate (O_EXCL fails for the loser, which
        moves to the next), so each gets a DISTINCT name — no last-writer-wins
        clobber. The caller renames its temp file over the placeholder.
        """
        p = Path(name)
        base, ext = str(p.with_suffix("")), p.suffix
        candidate = name
        counter = 0
        while True:
            full_path = self._path(candidate)
            Path(full_path).parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(full_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                return candidate, full_path
            except FileExistsError:
                counter += 1
                candidate = f"{base}_{counter}{ext}"
                if counter > 10000:
                    raise RuntimeError(f"Could not find available name for {name}")

    def get_available_name(self, name: str) -> str:
        """Return a free name (name, name_1, …) if the file already exists.

        Best-effort check-then-return (a small TOCTOU window remains). The save
        path does NOT use this — it uses _reserve_available_path, which claims
        the name atomically and is race-free under concurrent same-name saves.
        This is for callers that only need a name suggestion.
        """
        if not Path(self._path(name)).exists():
            return name

        # Split name into base and extension
        p = Path(name)
        base, ext = str(p.with_suffix("")), p.suffix
        counter = 1
        while True:
            candidate = f"{base}_{counter}{ext}"
            if not Path(self._path(candidate)).exists():
                return candidate
            counter += 1
            if counter > 10000:
                raise RuntimeError(f"Could not find available name for {name}")


class MemoryStorage(Storage):
    """In-memory file storage for testing.

    Files are stored in a dict. Thread-safe.
    """

    def __init__(self, base_url: str = "/media/"):
        self.base_url = base_url.rstrip("/") + "/"
        self._files: dict[str, bytes] = {}
        self._lock = threading.Lock()

    async def save(self, name: str, content: bytes) -> str:
        name = self.get_available_name(name)
        with self._lock:
            self._files[name] = content
        return name

    async def open(self, name: str) -> bytes:
        with self._lock:
            if name not in self._files:
                raise FileNotFoundError(f"File not found: {name}")
            return self._files[name]

    async def delete(self, name: str):
        with self._lock:
            self._files.pop(name, None)

    async def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._files

    def url(self, name: str) -> str:
        return f"{self.base_url}{name}"

    async def listdir(self, path: str = "") -> tuple[list[str], list[str]]:
        prefix = path.rstrip("/") + "/" if path else ""
        dirs = set()
        files = []
        with self._lock:
            for name in sorted(self._files.keys()):
                if not name.startswith(prefix):
                    continue
                remainder = name[len(prefix) :]
                if "/" in remainder:
                    dirs.add(remainder.split("/")[0])
                else:
                    files.append(remainder)
        return sorted(dirs), files

    async def size(self, name: str) -> int:
        with self._lock:
            if name not in self._files:
                raise FileNotFoundError(f"File not found: {name}")
            return len(self._files[name])

    def get_available_name(self, name: str) -> str:
        with self._lock:
            if name not in self._files:
                return name
            p = Path(name)
            base, ext = str(p.with_suffix("")), p.suffix
            counter = 1
            while True:
                candidate = f"{base}_{counter}{ext}"
                if candidate not in self._files:
                    return candidate
                counter += 1

    def clear(self):
        """Remove all stored files."""
        with self._lock:
            self._files.clear()


# ---------------------------------------------------------------------------
# Global storage instance
# ---------------------------------------------------------------------------

_default_storage: Storage | None = None


def get_storage() -> Storage:
    """Get the global default storage backend."""
    if _default_storage is None:
        raise RuntimeError(
            "No storage configured. Set up storage via set_storage() or app config."
        )
    return _default_storage


def set_storage(storage: Storage):
    """Set the global default storage backend."""
    global _default_storage
    _default_storage = storage


# A path-traversal / suspicious-file rejection surfaces as a 400, not a 500.
from hyperdjango.exceptions import register_exception_status as _register_exc_status

_register_exc_status(SuspiciousFileOperation, 400)
