"""
In-memory static file cache for development.

Inspired by merjs's static.zig — loads static files into memory
on first access for fast serving during development.

Usage:
    # settings.py
    HYPERDJANGO_STATIC_CACHE = True  # Only in DEBUG mode

    # Or use directly
    from hyperdjango.streaming.static_cache import InMemoryStaticCache
    cache = InMemoryStaticCache(['/path/to/static'])
"""

import mimetypes
import threading
from pathlib import Path


class InMemoryStaticCache:
    """Caches static files in memory for fast serving during development.

    Thread-safe. Files are loaded lazily on first access and never evicted.
    """

    def __init__(self, static_dirs=None):
        self._cache = {}
        self._lock = threading.Lock()
        self._static_dirs = static_dirs or []

    def get(self, path):
        """Get a cached file by path.

        Args:
            path: Relative path to the static file.

        Returns:
            (content_bytes, content_type) tuple, or None if not found.
        """
        if path in self._cache:
            return self._cache[path]

        # Try to find and cache the file
        for static_dir in self._static_dirs:
            full_path = Path(static_dir) / path
            if full_path.is_file():
                return self._load_and_cache(path, full_path)

        return None

    def _load_and_cache(self, path, full_path):
        """Load a file into the cache."""
        with self._lock:
            # Double-check after acquiring lock
            if path in self._cache:
                return self._cache[path]

            content = full_path.read_bytes()
            content_type = (
                mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
            )
            entry = (content, content_type)
            self._cache[path] = entry
            return entry

    def preload(self):
        """Preload all static files into memory."""
        for static_dir in self._static_dirs:
            static_path = Path(static_dir)
            if not static_path.exists():
                continue
            for file_path in static_path.rglob("*"):
                if file_path.is_file():
                    rel_path = str(file_path.relative_to(static_path))
                    self._load_and_cache(rel_path, file_path)

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()

    @property
    def size(self):
        """Number of cached files."""
        return len(self._cache)

    @property
    def total_bytes(self):
        """Total bytes of cached content."""
        return sum(len(content) for content, _ in self._cache.values())
