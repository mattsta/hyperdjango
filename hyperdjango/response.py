"""
HTTP Response object with ergonomic static constructors.

No Django dependency. Supports JSON, HTML, text, redirect, streaming,
and file responses.
"""

import hashlib
import http
import mimetypes
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from hyperdjango.native import fast_json_dumps
from hyperdjango.types import JSONValue

# Files at or below this size are read fully into memory (the fast buffered
# path); larger files are streamed from disk in bounded chunks so serving a big
# download never pins the whole file in RAM. See ``Response.file``.
_FILE_STREAM_THRESHOLD = 8 * 1024 * 1024  # 8 MiB
_FILE_CHUNK = 64 * 1024


# C0 controls + DEL, minus CR/LF which get truncation (not stripping) below.
_HEADER_CTL_TABLE = {c: None for c in range(0x20) if c not in (0x0D, 0x0A)}
_HEADER_CTL_TABLE[0x7F] = None


def _sanitize_header(value: str) -> str:
    """Neutralize header-injection vectors in a caller-supplied value.

    CR/LF TRUNCATES: everything after a header-split attempt is attacker
    payload, not part of the value — stripping used to concatenate
    "v1\\r\\nX-Injected: x" into the single token "v1X-Injected: x", which
    then reflected attacker text into error details. Remaining C0 controls
    and DEL are stripped so values landing in logs or echoed headers can't
    carry NUL/TAB smuggling. Printable values return unchanged (fast path).
    """
    if value.isprintable():
        return value
    r = value.find("\r")
    n = value.find("\n")
    cut = min(x for x in (r, n) if x != -1) if (r != -1 or n != -1) else -1
    if cut != -1:
        value = value[:cut]
    return value.translate(_HEADER_CTL_TABLE)


# Cookies are assembled as "k=v; Path=..; Domain=.." strings, so a component
# carrying ';' would inject additional cookie ATTRIBUTES (Domain=evil.com,
# dropping Secure, …) and a CR/LF would split into new response headers. Strip
# all three from EVERY caller-supplied component (name, value, path, domain).
_COOKIE_UNSAFE = ("\r", "\n", ";")

# SameSite is a fixed enum; anything else is treated as the safe default so a
# forged value can't smuggle attributes through the SameSite= slot.
_COOKIE_SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None"}

# Cookie VALUES are percent-encoded on write so they round-trip through the
# read path (native parse_cookies percent-DECODES %XX). The safe set is the
# RFC 6265 cookie-octet punctuation MINUS '%' — '%' must itself be encoded
# (→ %25) so a literal '%XX' in a value isn't mis-decoded on read. Space,
# '"', ',', ';', '\\' and control chars are NOT safe → they get %-encoded,
# which also neutralizes attribute/header injection. Alphanumerics and this
# punctuation pass through unchanged, so base62 signed tokens are untouched.
_COOKIE_VALUE_SAFE = "!#$&'()*+/:<=>?@[]^`{|}~"


def _sanitize_cookie_component(value: str) -> str:
    """Strip CR/LF and ';' from a Set-Cookie component (name/path/domain)."""
    for ch in _COOKIE_UNSAFE:
        value = value.replace(ch, "")
    return value


def _encode_cookie_value(value: str) -> str:
    """Percent-encode a cookie value so it round-trips through the percent-
    decoding read path (request.cookies). Symmetric inverse of the native
    parse_cookies decoder."""
    return quote(value, safe=_COOKIE_VALUE_SAFE)


def _sse_field(value) -> str:
    """Strip CR/LF from an SSE field value (event/id/retry) — a newline would
    inject a spurious SSE field or a fake event. Same CRLF policy as headers."""
    return _sanitize_header(str(value))


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 If-None-Match: '*' or a (possibly weak) list containing ``etag``.

    Mirrors ``hyperdjango.app._etag_matches`` (comma-split + weak-validator
    handling) so conditional GETs on a ``Response`` behave identically to the
    native static path — NOT a substring test (``"1"`` must not match ``"12"``).
    Kept local to avoid a response.py → app.py import cycle.
    """
    inm = if_none_match.strip()
    if inm == "*":
        return True
    for token in inm.split(","):
        token = token.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag:
            return True
    return False


def _encode_location(url: str) -> str:
    """IRI→URI encode a redirect ``Location`` so it is always Latin-1-encodable.

    HTTP header values are Latin-1 on the wire (the ASGI ``send`` path encodes
    them as ``latin-1``, and the native serializer likewise). A raw Location
    carrying non-ASCII characters — a non-ASCII path or query such as
    ``/café/naïve?q=résumé#ünicode`` — raises ``UnicodeEncodeError`` at
    serialize time, turning a redirect into a 500.

    Per RFC 3987 we map the IRI to a URI by percent-encoding the UTF-8 bytes of
    every non-ASCII character while PRESERVING URL structure:

      * ``%`` is in the safe set, so input that is ALREADY percent-encoded is
        not double-encoded (``%C3%A9`` stays ``%C3%A9``);
      * the reserved / sub-delimiter / structural characters
        (``/ ? # [ ] = : ; $ & ( ) + , ! * @ ' ~``) are left intact, so the
        path / query / fragment boundaries and separators survive.

    ASCII URLs whose characters are all safe or unreserved pass through
    unchanged (the standard IRI→URI safe set per RFC 3987).
    """
    return quote(url, safe="/#%[]=:;$&()+,!?*@'~")


# Non-``text/`` content types that are still text/markup on the wire and so
# need an explicit ``; charset=utf-8``. ``text/*`` is handled by prefix in
# ``_with_charset``. Mirrors the text members of
# ``staticfiles._COMPRESSIBLE_TYPES`` for cross-path consistency.
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/manifest+json",
        "image/svg+xml",
    }
)


def _with_charset(content_type: str) -> str:
    """Append ``; charset=utf-8`` to a text-based content type lacking a charset.

    Text/markup types — ``text/*`` and the known non-``text/`` text families
    like ``application/javascript`` and ``image/svg+xml`` (XML) — are UTF-8 on
    the wire, so a bare guessed type such as ``text/css`` must carry a charset
    or clients may misdecode it. Binary types (images, video,
    ``application/octet-stream``) are returned unchanged, and a type that
    already declares a charset is left as-is.
    """
    if "charset=" in content_type.lower():
        return content_type
    base = content_type.split(";", 1)[0].strip().lower()
    if base.startswith("text/") or base in _TEXT_CONTENT_TYPES:
        return f"{content_type}; charset=utf-8"
    return content_type


def _content_disposition_attachment(filename: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` value for ``filename``.

    A raw ``filename="..."`` breaks when the name contains a ``"`` (quote-escape)
    and raises ``UnicodeEncodeError`` on the ASGI ``latin-1`` header encode for a
    non-latin-1 name. We escape quotes/backslashes for the plain ``filename=``
    ASCII fallback and always add an RFC 5987 ``filename*=UTF-8''...`` parameter
    (percent-encoded) so modern clients recover the exact UTF-8 name.
    """
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    # ASCII/latin-1 fallback for the plain filename= parameter (header values are
    # latin-1 on the ASGI path); non-latin-1 codepoints are dropped here and
    # carried losslessly by filename* below.
    ascii_fallback = escaped.encode("latin-1", "ignore").decode("latin-1")
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


# Sentinel: a syntactically-valid Range that cannot be satisfied → 416.
_RANGE_UNSATISFIABLE = object()


def _parse_byte_range(range_header: str | None, size: int):
    """Parse a single RFC 7233 ``Range: bytes=...`` header against ``size``.

    Returns an inclusive ``(start, end)`` tuple for a satisfiable range,
    ``_RANGE_UNSATISFIABLE`` for a well-formed but out-of-bounds range (→ 416),
    or ``None`` to ignore the Range and serve the full body (→ 200): no header,
    an unsupported unit, MULTIPLE ranges (multipart/byteranges is intentionally
    not supported — a single range covers media seeking + resumable downloads),
    or malformed syntax. Only ``bytes`` is a valid unit per the RFC.
    """
    if not range_header:
        return None
    spec = range_header.strip()
    if not spec.lower().startswith("bytes="):
        return None
    spec = spec[6:].strip()
    if "," in spec or "-" not in spec:  # multi-range or malformed → ignore
        return None
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s == "":
            # Suffix range: last N bytes ("bytes=-500").
            if end_s == "":
                return None
            n = int(end_s)
            if n <= 0:
                return _RANGE_UNSATISFIABLE
            if size <= 0:
                return _RANGE_UNSATISFIABLE
            return (max(0, size - n), size - 1)
        start = int(start_s)
        end = int(end_s) if end_s != "" else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return _RANGE_UNSATISFIABLE
    return (start, min(end, size - 1))


@dataclass(slots=True)
class Response:
    """HTTP response.

    Use the static constructors for ergonomic response creation:
        Response.json(data)
        Response.html(content)
        Response.text(content)
        Response.redirect(url)
    """

    body: bytes = b""
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    _streaming: bool = field(default=False, init=False, repr=False)
    _stream_iter: AsyncIterator[bytes] | None = field(
        default=None, init=False, repr=False
    )
    # Set-Cookie is modelled as a LIST (one entry per cookie), not a single
    # header value: multiple cookies are DISTINCT header lines on the wire, and
    # cramming them into one dict value forced a CRLF into a single header value
    # (rejected by h11/uvicorn on the ASGI path). ``send()`` and the native tuple
    # serializer emit one line per entry. ``headers['set-cookie']`` is kept as a
    # joined compat mirror so callers/tests that read it directly still work.
    _cookies: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")
        elif not isinstance(self.body, bytes):
            self.body = str(self.body).encode("utf-8")

        # Sanitize all header values against CRLF injection
        self.headers = {k: _sanitize_header(v) for k, v in self.headers.items()}

        if self.content_type:
            self.headers.setdefault("content-type", _sanitize_header(self.content_type))
        elif "content-type" not in self.headers:
            self.headers["content-type"] = "text/plain; charset=utf-8"

    # --- Static constructors ---

    @classmethod
    def json(
        cls, data: JSONValue, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        """Create a JSON response.

        Uses SIMD-accelerated JSON serialization when the Zig native
        extension is available. Falls back to optimized Python JSON.
        """
        body = fast_json_dumps(data)
        return cls(
            body=body,
            status=status,
            headers=dict(headers or {}),
            content_type="application/json; charset=utf-8",
        )

    @classmethod
    def error(
        cls,
        status: int,
        detail: str = "",
        *,
        errors: JSONValue | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """Create a unified error response — body ``{"detail", "status"}`` (plus
        an optional ``"errors"`` map for field validation).

        The one ergonomic way to RETURN an error from a handler or middleware::

            return Response.error(404, "Order not found")
            return Response.error(422, "Invalid", errors={"email": ["required"]})

        To signal an error from deeper code (a service, serializer, guard),
        ``raise HTTPException(status, detail)`` instead — the framework maps it
        to this exact same body. ``detail`` defaults to the standard reason
        phrase for ``status``.
        """
        if not detail:
            try:
                detail = http.HTTPStatus(status).phrase
            except ValueError:
                detail = "Error"
        body: dict = {"detail": detail, "status": status}
        if errors is not None:
            body["errors"] = errors
        return cls.json(body, status=status, headers=headers)

    @classmethod
    def html(
        cls, content: str, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        """Create an HTML response."""
        return cls(
            body=content.encode("utf-8"),
            status=status,
            headers=dict(headers or {}),
            content_type="text/html; charset=utf-8",
        )

    @classmethod
    def text(
        cls, content: str, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        """Create a plain text response."""
        return cls(
            body=content.encode("utf-8"),
            status=status,
            headers=dict(headers or {}),
            content_type="text/plain; charset=utf-8",
        )

    @classmethod
    def redirect(
        cls, url: str, status: int = 302, headers: dict[str, str] | None = None
    ) -> Response:
        """Create a redirect response.

        The ``Location`` value is IRI→URI encoded (see ``_encode_location``) at
        this single choke point so every redirect feeder — ``shortcuts.redirect``
        and ``redirects.RedirectMiddleware`` both route through here — gets a
        Latin-1-encodable header without duplicating the encoding.
        """
        h = dict(headers or {})
        h["location"] = _encode_location(url)
        return cls(body=b"", status=status, headers=h)

    @classmethod
    def stream(
        cls,
        iterator,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str = "text/plain",
    ) -> Response:
        """Create a streaming response."""
        resp = cls(
            body=b"",
            status=status,
            headers=dict(headers or {}),
            content_type=content_type,
        )
        resp._streaming = True
        resp._stream_iter = iterator
        return resp

    @classmethod
    def empty(
        cls, status: int = 204, headers: dict[str, str] | None = None
    ) -> Response:
        """Create an empty response."""
        return cls(body=b"", status=status, headers=dict(headers or {}))

    @classmethod
    def sse(
        cls, iterator, status: int = 200, headers: dict[str, str] | None = None
    ) -> Response:
        """Create a Server-Sent Events streaming response."""

        async def sse_stream():
            async for event in iterator:
                if isinstance(event, dict):
                    parts = []
                    # Strip CR/LF from the field VALUES — a newline in event/id/
                    # retry would inject spurious SSE fields or a fake event.
                    # (data is line-split into `data:` lines below, so it's safe.)
                    if "event" in event:
                        parts.append(f"event: {_sse_field(event['event'])}")
                    if "id" in event:
                        parts.append(f"id: {_sse_field(event['id'])}")
                    if "retry" in event:
                        parts.append(f"retry: {_sse_field(event['retry'])}")
                    data = event.get("data", "")
                    for line in str(data).split("\n"):
                        parts.append(f"data: {line}")
                    parts.append("")
                    yield "\n".join(parts) + "\n"
                elif isinstance(event, str):
                    yield f"data: {event}\n\n"
                else:
                    yield f"data: {event}\n\n"

        resp = cls.stream(
            sse_stream(),
            status=status,
            headers=headers,
            content_type="text/event-stream",
        )
        resp.headers["cache-control"] = "no-cache"
        resp.headers["connection"] = "keep-alive"
        return resp

    @classmethod
    def file(
        cls,
        path: str,
        content_type: str | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        request=None,
    ) -> Response:
        """Serve a file from disk with automatic content-type detection.

        Files at or below ``_FILE_STREAM_THRESHOLD`` (8 MiB) are read fully into
        memory (fast buffered path with a real ``Content-Length``). Larger files
        are STREAMED from disk in ``_FILE_CHUNK`` blocks via the chunked path, so
        a big download is never materialized in RAM at once. Streamed responses
        are framed with ``Transfer-Encoding: chunked`` (no ``Content-Length``).

        Pass ``request=`` to enable HTTP ``Range`` support (RFC 7233): a valid
        single byte-range yields ``206 Partial Content`` with ``Content-Range``,
        an unsatisfiable one yields ``416``, and every response advertises
        ``Accept-Ranges: bytes``. This is what makes ``<video>``/``<audio>``
        seeking and resumable downloads work; without a ``request`` the whole
        file is served. Only a single range is honored;
        multi-range requests fall back to the full body.
        """
        file_path = Path(path)
        try:
            st = file_path.stat()
        except OSError:
            return cls(body=b"Not Found", status=404, content_type="text/plain")
        if not content_type:
            content_type = (
                mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            )
            # Guessed text/markup types (text/*, application/javascript,
            # image/svg+xml, …) get an explicit charset; binary stays bare.
            content_type = _with_charset(content_type)

        size = st.st_size
        range_spec = (
            _parse_byte_range(request.headers.get("range"), size)
            if request is not None
            else None
        )

        # Well-formed but out-of-bounds Range → 416, reporting the true size.
        if range_spec is _RANGE_UNSATISFIABLE:
            h = dict(headers or {})
            h["accept-ranges"] = "bytes"
            h["content-range"] = f"bytes */{size}"
            h.setdefault("x-content-type-options", "nosniff")
            return cls(body=b"", status=416, headers=h, content_type=content_type)

        # Satisfiable single range → 206 Partial Content over just that window.
        if range_spec is not None:
            start, end = range_spec
            length = end - start + 1
            rh = dict(headers or {})
            rh["accept-ranges"] = "bytes"
            rh["content-range"] = f"bytes {start}-{end}/{size}"
            rh.setdefault("x-content-type-options", "nosniff")
            if length > _FILE_STREAM_THRESHOLD:
                # Large window (e.g. "seek to N, play to end") — stream it bounded
                # instead of buffering; never materialize the whole slice in RAM.
                async def _range_chunks(fp=file_path, s=start, n=length):
                    remaining = n
                    with fp.open("rb") as fh:
                        fh.seek(s)
                        while remaining > 0:
                            chunk = fh.read(min(_FILE_CHUNK, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk

                return cls.stream(
                    _range_chunks(), status=206, headers=rh, content_type=content_type
                )
            with file_path.open("rb") as fh:
                fh.seek(start)
                content = fh.read(length)
            rh["content-length"] = str(len(content))
            return cls(body=content, status=206, headers=rh, content_type=content_type)

        if size > _FILE_STREAM_THRESHOLD:
            # Stream large files in bounded chunks instead of buffering the whole
            # file. Reads run one chunk at a time as the iterator is driven (by
            # the native chunked-send loop or the ASGI send loop).
            async def _file_chunks(fp=file_path):
                with fp.open("rb") as fh:
                    while True:
                        chunk = fh.read(_FILE_CHUNK)
                        if not chunk:
                            break
                        yield chunk

            stream_headers = dict(headers or {})
            # Served INLINE with a guessed content-type — stop the browser from
            # MIME-sniffing a user-uploaded .svg/.html into active content
            # (stored-XSS in this origin). Caller override wins.
            stream_headers.setdefault("x-content-type-options", "nosniff")
            stream_headers.setdefault("accept-ranges", "bytes")
            return cls.stream(
                _file_chunks(),
                status=status,
                headers=stream_headers,
                content_type=content_type,
            )

        content = file_path.read_bytes()
        h = dict(headers or {})
        h["content-length"] = str(len(content))
        # Served INLINE with a guessed content-type — block MIME-sniffing so a
        # user-uploaded .svg/.html can't execute as active content (stored XSS).
        h.setdefault("x-content-type-options", "nosniff")
        # Advertise range support so clients know they can resume / seek.
        h.setdefault("accept-ranges", "bytes")
        return cls(body=content, status=status, headers=h, content_type=content_type)

    @classmethod
    def attachment(
        cls,
        path: str,
        filename: str = None,
        headers: dict[str, str] | None = None,
        request=None,
    ) -> Response:
        """Serve a file as a downloadable attachment.

        Pass ``request=`` to enable ``Range`` support so interrupted downloads can
        be resumed (see :meth:`file`).
        """
        resp = cls.file(path, headers=headers, request=request)
        if resp.status in (404, 416):
            return resp
        if not filename:
            filename = Path(path).name
        resp.headers["content-disposition"] = _content_disposition_attachment(filename)
        # Defense-in-depth alongside the attachment disposition: block
        # MIME-sniffing so the download can't be reinterpreted as active
        # content. (file() already sets this for the 200 path; make it explicit
        # here so the guarantee survives independent of file()'s internals.)
        resp.headers.setdefault("x-content-type-options", "nosniff")
        return resp

    @property
    def is_streaming(self):
        return self._streaming

    def set_etag(self, etag=None):
        """Set ETag header. Auto-generates from body hash if not provided."""
        if etag is None:
            etag = hashlib.md5(self.body).hexdigest()
        self.headers["etag"] = f'"{etag}"'
        return self

    def cache_control(
        self,
        public=False,
        private=False,
        no_cache=False,
        no_store=False,
        max_age=None,
        s_maxage=None,
    ):
        """Set Cache-Control header."""
        parts = []
        if public:
            parts.append("public")
        if private:
            parts.append("private")
        if no_cache:
            parts.append("no-cache")
        if no_store:
            parts.append("no-store")
        if max_age is not None:
            parts.append(f"max-age={max_age}")
        if s_maxage is not None:
            parts.append(f"s-maxage={s_maxage}")
        self.headers["cache-control"] = ", ".join(parts)
        return self

    def check_not_modified(self, request) -> bool:
        """Check If-None-Match against ETag. Returns True and sets 304 if match."""
        etag = self.headers.get("etag")
        if not etag:
            return False
        if_none_match = request.headers.get("if-none-match", "")
        if not if_none_match:
            return False
        # RFC 9110 comma-split + weak-validator match — NOT a substring test
        # (`"1"` must not match `"12"`). Shared semantics with the static path.
        if _etag_matches(if_none_match, etag):
            self.status = 304
            self.body = b""
            return True
        return False

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: int | None = None,
        path: str = "/",
        domain: str | None = None,
        httponly: bool = True,
        secure: bool = False,
        samesite: str = "Lax",
    ):
        """Set a response cookie.

        Args:
            key: Cookie name.
            value: Cookie value.
            max_age: Lifetime in seconds (None = session cookie).
            path: Cookie path scope.
            domain: Cookie domain scope.
            httponly: Not accessible via JavaScript.
            secure: HTTPS-only cookie.
            samesite: "Strict", "Lax", or "None".
        """
        # Sanitize EVERY caller-supplied component so neither a CR/LF (header
        # injection) nor a ';' (cookie-attribute injection, e.g. a forged
        # Domain= that re-scopes the cookie or drops Secure) can be smuggled in.
        key = _sanitize_cookie_component(str(key))
        # Percent-encode the value (symmetric with the percent-decoding read
        # path) so it round-trips losslessly and can't smuggle ';'/CRLF/space.
        value = _encode_cookie_value(str(value))
        path = _sanitize_cookie_component(str(path))
        # SameSite is an allowlisted enum; an unrecognized value falls back to
        # the safe default rather than being interpolated verbatim.
        samesite = (
            _COOKIE_SAMESITE.get(str(samesite).lower(), "Lax") if samesite else samesite
        )
        if samesite and samesite.lower() == "none":
            secure = True
        cookie = f"{key}={value}; Path={path}; SameSite={samesite}"
        if max_age is not None:
            cookie += f"; Max-Age={int(max_age)}"
        if domain:
            cookie += f"; Domain={_sanitize_cookie_component(str(domain))}"
        if httponly:
            cookie += "; HttpOnly"
        if secure:
            cookie += "; Secure"
        # LIST model: each cookie is a distinct header line. Keep a joined
        # compat mirror in headers['set-cookie'] (the same "\r\nset-cookie: "
        # separator the TestClient splits on) so direct readers keep working;
        # send()/native emit one line per entry from self._cookies.
        self._cookies.append(cookie)
        self.headers["set-cookie"] = "\r\nset-cookie: ".join(self._cookies)
        return self

    def _cookie_lines(self) -> list[str]:
        """Return one cookie string per Set-Cookie line to emit.

        Prefers the structured ``_cookies`` list (set via ``set_cookie``). Falls
        back to splitting a directly-assigned ``headers['set-cookie']`` value for
        callers that bypass ``set_cookie`` and write the header themselves.
        """
        if self._cookies:
            return self._cookies
        raw = self.headers.get("set-cookie")
        if raw:
            return raw.split("\r\nset-cookie: ")
        return []

    def delete_cookie(self, key: str, path: str = "/", domain: str | None = None):
        """Delete a cookie by setting max_age=0."""
        return self.set_cookie(
            key, "", max_age=0, path=path, domain=domain, httponly=False
        )

    def __repr__(self):
        return f"Response(status={self.status}, content_type={self.headers.get('content-type', '?')})"

    # --- ASGI interface ---

    async def send(self, send_func):
        """Send this response via ASGI send."""
        # Set-Cookie is emitted as ONE header tuple PER cookie (never a single
        # value with embedded CRLF, which h11/uvicorn reject). All other headers
        # pass through unchanged.
        # Re-sanitize header values here (not only at construction): a header
        # assigned AFTER __post_init__ — resp.headers["x"] = user_value — would
        # otherwise reach the wire unchecked. The native emit path already
        # sanitizes; do the same on the ASGI path so CRLF header injection is
        # blocked by us, not merely by the downstream server's strictness.
        headers = [
            (
                _sanitize_header(k).encode("latin-1"),
                _sanitize_header(v).encode("latin-1"),
            )
            for k, v in self.headers.items()
            if k.lower() != "set-cookie"
        ]
        for cookie in self._cookie_lines():
            headers.append((b"set-cookie", cookie.encode("latin-1")))

        if self._streaming:
            await send_func(
                {
                    "type": "http.response.start",
                    "status": self.status,
                    "headers": headers,
                }
            )
            # Drive the iterator under try/finally so the async generator (and,
            # for Response.file, the fd its `with open(...)` holds) is released
            # if the client disconnects or send_func raises mid-stream. Without
            # this the suspended generator lingers until GC, leaking the fd.
            stream = self._stream_iter
            try:
                async for chunk in stream:
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    await send_func(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
                await send_func(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                )
            finally:
                # aclose() is idempotent (a no-op on an already-exhausted gen)
                # and exists only on async generators — Response.stream also
                # accepts a plain async iterator — so probe for it. On normal
                # completion the gen is already closed; this only bites on the
                # abort/exception path.
                # dynamic-attr: aclose is an optional async-generator capability on the AsyncIterator protocol, not guaranteed on every stream iterator
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
        else:
            await send_func(
                {
                    "type": "http.response.start",
                    "status": self.status,
                    "headers": headers,
                }
            )
            await send_func(
                {
                    "type": "http.response.body",
                    "body": self.body,
                    "more_body": False,
                }
            )
