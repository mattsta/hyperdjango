"""
ZigHandler — Bridge between Zig HTTP server and Django's WSGI middleware chain.

Receives raw HTTP data from Zig, builds a Django HttpRequest, runs it through
Django's full middleware chain (SecurityMiddleware, SessionMiddleware, CSRF, etc.),
and returns a pre-formatted response tuple for Zig to send directly.

The response headers are pre-formatted as a single string ("\r\nKey: Value\r\n...")
so Zig can write them without any parsing — single memcpy into the response buffer.
"""

import io

from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpRequest, QueryDict

from hyperdjango.native import parse_cookies
from hyperdjango.response import _encode_cookie_value

# Framing / hop-by-hop headers that the Zig layer owns exclusively. Django (or a
# middleware, e.g. ConditionalGetMiddleware or FileResponse) may set its own
# Content-Length / Transfer-Encoding / Connection; passing those through would
# collide with the Content-Length + Connection that server.zig emits itself,
# producing two conflicting Content-Length headers (a response-smuggling
# primitive) or a bogus Connection echo. Let Zig be the single source of framing.
_FRAMING_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})


def _format_headers(response):
    """Pre-format a Django response's headers into Zig's "\\r\\nKey: Value" block.

    Framing/hop-by-hop headers (Content-Length/Transfer-Encoding/Connection) are
    dropped so Zig owns framing (no duplicate/conflicting Content-Length), and
    Set-Cookie (kept separate from response.items()) is appended per cookie.
    """
    header_parts = []
    for key, value in response.items():
        if key.lower() in _FRAMING_HEADERS:
            continue
        header_parts.append(f"\r\n{key}: {value}")
    # Add Set-Cookie headers (response.cookies is separate from response.items()).
    # The WSGI-compat cookie container octal-escapes special-char values (\ooo);
    # the platform reads cookies with its native PERCENT codec (request.cookies),
    # so re-encode each value through the one cookie authority (_encode_cookie_value)
    # before emitting. Base64/hex values (session, CSRF) are unchanged; only
    # special-char values gain a codec that the native reader round-trips.
    for cookie in response.cookies.values():
        # coded_value is read-only; set() rewrites (key, raw value, coded value).
        cookie.set(cookie.key, cookie.value, _encode_cookie_value(cookie.value))
        header_parts.append(f"\r\nSet-Cookie: {cookie.output(header='').strip()}")
    return "".join(header_parts)


def _make_stream_pull(streaming_content):
    """Build the sync pull callable Zig's chunked-send loop drives one chunk/step.

    Django's ``streaming_content`` (StreamingHttpResponse/FileResponse under WSGI)
    is a SYNC iterator of bytes. Each ``pull()`` advances it exactly one step and
    returns the next chunk as ``bytes`` (``None`` at ``StopIteration``), so the
    native chunked path streams it incrementally instead of joining the whole
    (possibly unbounded) generator into memory.
    """
    it = iter(streaming_content)

    def pull():
        try:
            chunk = next(it)
        except StopIteration:
            return None
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        return bytes(chunk)

    return pull


def format_response(response):
    """Flatten a Django response into the (status, headers_str, body) tuple Zig expects.

    Extracted from ZigHandler.__call__ so the framing-header filtering and the
    streaming-body fallback are unit-testable without standing up the full WSGI
    middleware chain (see scripts/test_http_head_framing_r7.py).

    - Framing/hop-by-hop headers (Content-Length/Transfer-Encoding/Connection) are
      dropped so Zig owns framing (no duplicate/conflicting Content-Length).
    - StreamingHttpResponse/FileResponse expose no `.content` — fall back to
      joining `.streaming_content` so the body is never silently dropped.

    This BUFFERED form is retained for callers/tests that want a materialized
    body; the live ``ZigHandler.__call__`` path streams such responses
    incrementally instead (see below).
    """
    headers_str = _format_headers(response)

    if hasattr(response, "content"):
        body_bytes = response.content
    elif hasattr(response, "streaming_content"):
        body_bytes = b"".join(response.streaming_content)
    else:
        body_bytes = b""

    return (response.status_code, headers_str, body_bytes)


class ZigHandler:
    """WSGI bridge: Zig HTTP → Django middleware → Zig response.

    Registered as the Django catch-all handler via _server_set_django_handler().
    Called for every request that doesn't match a registered Zig route.
    """

    def __init__(self, server_name="localhost", server_port="8000"):
        self._django_handler = WSGIHandler()
        self._django_handler.load_middleware()
        self._server_name = server_name
        self._server_port = server_port

    def __call__(self, method, path, headers, body, query_string):
        """Handle a request from the Zig HTTP server.

        Args:
            method: HTTP method string (GET, POST, etc.)
            path: URL path
            headers: Dict of HTTP headers {name: value}
            body: Request body bytes
            query_string: Query string (without leading ?)

        Returns:
            (status_code, headers_str, body_bytes) tuple.
            headers_str is pre-formatted: "\\r\\nContent-Type: text/html\\r\\nSet-Cookie: ..."
        """
        request = self._build_request(method, path, headers, body, query_string)
        response = self._django_handler.get_response(request)

        # Streaming responses (StreamingHttpResponse/FileResponse — no `.content`,
        # only `.streaming_content`) are driven INCREMENTALLY via the Zig
        # chunked-send path: return the streaming 4-tuple
        #   (status, headers_str, b"", pull)
        # whose callable yields one chunk per step. Joining an unbounded generator
        # here would buffer the whole stream in RAM (and hang a worker on an
        # infinite stream) — the DoS this replaces.
        if not hasattr(response, "content") and hasattr(response, "streaming_content"):
            headers_str = _format_headers(response)
            return (
                response.status_code,
                headers_str,
                b"",
                _make_stream_pull(response.streaming_content),
            )

        # Pre-format the buffered response into (status, headers_str, body) for
        # Zig. headers_str is "\r\nKey: Value\r\nKey: Value" (no trailing \r\n);
        # Zig owns Content-Length/Connection framing (see format_response).
        return format_response(response)

    def _build_request(self, method, path, headers, body, query_string):
        """Convert Zig request data into a Django HttpRequest."""
        request = HttpRequest()
        request.method = method
        request.path = path
        request.path_info = path
        request.META = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query_string or "",
            "SERVER_NAME": self._server_name,
            "SERVER_PORT": self._server_port,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.input": io.BytesIO(body or b""),
            "wsgi.url_scheme": "http",
        }

        # Convert headers to Django META format.
        # Drop header names containing '_': HTTP forbids '_' in header names,
        # but the HTTP_<NAME> mapping collapses both 'X-Foo' and 'X_Foo' to
        # 'HTTP_X_FOO', letting attackers spoof trusted headers
        # (X-Forwarded-For, X-Real-IP) when behind certain reverse proxies.
        if headers:
            for key, value in headers.items():
                if "_" in key:
                    continue
                meta_key = "HTTP_" + key.upper().replace("-", "_")
                if key.lower() == "content-type":
                    meta_key = "CONTENT_TYPE"
                elif key.lower() == "content-length":
                    meta_key = "CONTENT_LENGTH"
                elif key.lower() == "host":
                    request.META["SERVER_NAME"] = value.split(":")[0]
                    if ":" in value:
                        request.META["SERVER_PORT"] = value.split(":")[1]
                request.META[meta_key] = value

        # Populate COOKIES with the ONE native percent codec (the same
        # parse_cookies request.cookies uses), so cookies read here match the
        # percent encoding _format_headers / Response.set_cookie write. A bare
        # request object leaves COOKIES = {} and never parses HTTP_COOKIE, so this
        # both unifies the codec and wires request cookies for the middleware chain.
        cookie_header = request.META.get("HTTP_COOKIE", "")
        if cookie_header:
            request.COOKIES = parse_cookies(cookie_header)

        request.GET = QueryDict(query_string or "")
        if body:
            request._body = body
            content_type = request.META.get("CONTENT_TYPE", "")
            if "application/x-www-form-urlencoded" in content_type:
                request.POST = QueryDict(body)

        return request
