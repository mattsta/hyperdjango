"""Native log accelerators with source-only fallbacks — the bootstrap boundary.

``hyperdjango.logging`` sits on the import spine of the bootstrap tools: the
build front-end that PRODUCES the native extension (``hyper-build``), the test
runner, and the source-invariant gates that run where no compiled extension
can exist (the CI lint job, sdist builds, a fresh checkout). Those tools must
import cleanly before the extension is built, so this module is the ONE place
that touches the native extension on logging's behalf: it binds the
accelerated helpers when the extension is importable, and semantically
identical Python implementations when it is not — chosen once at import time,
never per call. The rest of the framework's runtime may hard-require the
native extension; logging must not.

Each fallback mirrors its Zig twin in ``zig/src/log_helpers.zig`` exactly;
a semantic change on either side must land on both.
"""

try:
    from hyperdjango._hyperdjango_native import _log_basename as log_basename
    from hyperdjango._hyperdjango_native import _log_module_name as log_module_name
    from hyperdjango._hyperdjango_native import _log_timestamp_iso as log_timestamp_iso
    from hyperdjango._hyperdjango_native import json_dumps_native as json_dumps
except ImportError:
    import json as _json

    def log_basename(path: str) -> str:
        """Substring after the last ``/`` or ``\\`` (whole path when neither
        occurs) — mirrors ``log_helpers.log_basename``."""
        cut = max(path.rfind("/"), path.rfind("\\")) + 1
        return path[cut:]

    def log_module_name(name: str) -> str:
        """Strip from the LAST dot (``foo.bar.py`` → ``foo.bar``; no dot →
        unchanged) — mirrors ``log_helpers.log_module_name``."""
        dot = name.rfind(".")
        return name if dot < 0 else name[:dot]

    def log_timestamp_iso() -> None:
        """No native timestamp available. Returning ``None`` (not bytes)
        routes JsonSink's existing non-bytes branch to the record's own
        ``time.isoformat()``."""
        return None

    def json_dumps(obj: object) -> str:
        return _json.dumps(obj, default=str)
