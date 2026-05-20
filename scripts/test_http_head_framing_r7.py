"""
Regression tests for HTTP response-side framing fixes (round 7).

Covers RFC 7230 §3.3 framing correctness on the response path:

  * handler.py must NOT emit Content-Length / Transfer-Encoding / Connection —
    Zig owns framing, so a Django response carrying its own Content-Length must
    yield ZERO Content-Length in the pre-formatted header string (Zig then adds
    exactly one → no duplicate/conflicting Content-Length smuggling primitive).
  * FileResponse / StreamingHttpResponse have no `.content`; their body must be
    recovered from `.streaming_content` and never silently dropped.
  * Ordinary headers, Set-Cookie, and status code are preserved.

These assertions exercise pure Python (hyperdjango.serving.handler.format_response)
and run WITHOUT the native module. The wire-level guarantees they imply are noted
at the bottom — those require the orchestrator's rebuilt .so to verify end-to-end.

Run standalone:  .venv/bin/python scripts/test_http_head_framing_r7.py
Or under the project runner / pytest.
"""

# hyper-test: unit

import io

from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        DEFAULT_CHARSET="utf-8",
        ALLOWED_HOSTS=["*"],
    )
    import django

    django.setup()

from django.http import (  # noqa: E402
    FileResponse,
    HttpResponse,
    StreamingHttpResponse,
)

from hyperdjango.serving.handler import format_response  # noqa: E402
from hyperdjango.testkit import check, finish, run_main  # noqa: E402

_FRAMING = ("content-length", "transfer-encoding", "connection")


def _header_names(headers_str):
    """Parse the "\\r\\nKey: Value" block into a list of lowercase header names."""
    names = []
    for line in headers_str.split("\r\n"):
        if not line:
            continue
        assert ": " in line, f"malformed header line: {line!r}"
        names.append(line.split(":", 1)[0].strip().lower())
    return names


def test_framing_headers_stripped():
    """A Django response with its own Content-Length must not re-emit it."""
    resp = HttpResponse(b"xyz")
    resp["Content-Length"] = "3"  # Django/middleware-set framing header
    resp["Connection"] = "keep-alive"
    resp["Transfer-Encoding"] = "chunked"
    resp["X-Custom"] = "keep-me"

    status, headers_str, body = format_response(resp)

    names = _header_names(headers_str)
    for framing in _FRAMING:
        assert framing not in names, f"{framing} must be stripped, got {names}"
    # Non-framing headers survive.
    assert "content-type" in names
    assert "x-custom" in names
    assert status == 200
    assert body == b"xyz"
    # Zig adds exactly one Content-Length from len(body); combined with zero here
    # that is exactly one on the wire.
    assert headers_str.lower().count("content-length") == 0


def test_streaming_body_not_dropped():
    """StreamingHttpResponse has no .content — body must come from streaming_content."""
    resp = StreamingHttpResponse(iter([b"chunk-a", b"chunk-b"]))
    assert not hasattr(resp, "content")  # invariant this fix depends on

    status, headers_str, body = format_response(resp)

    assert body == b"chunk-achunk-b", f"streaming body dropped: {body!r}"
    assert status == 200
    # Streaming responses must not leak Transfer-Encoding/Content-Length upstream.
    for framing in _FRAMING:
        assert framing not in _header_names(headers_str)


def test_fileresponse_body_and_content_length_stripped():
    """FileResponse auto-sets Content-Length in items(); it must be stripped, body kept."""
    payload = b"file-body-bytes"
    resp = FileResponse(io.BytesIO(payload))
    # Sanity: FileResponse really does expose Content-Length via items().
    assert any(k.lower() == "content-length" for k, _ in resp.items())

    status, headers_str, body = format_response(resp)

    assert body == payload, f"FileResponse body dropped: {body!r}"
    assert "content-length" not in _header_names(headers_str)
    assert status == 200


def test_set_cookie_and_status_preserved():
    resp = HttpResponse(b"ok", status=201)
    resp.set_cookie("sid", "abc123")

    status, headers_str, body = format_response(resp)

    assert status == 201
    assert body == b"ok"
    assert "set-cookie" in _header_names(headers_str)
    assert "sid=abc123" in headers_str


def main() -> bool:
    tests = [
        test_framing_headers_stripped,
        test_streaming_body_not_dropped,
        test_fileresponse_body_and_content_length_stripped,
        test_set_cookie_and_status_preserved,
    ]
    # Every test runs even after a failure — these are independent framing
    # facets and the full picture is more useful than the first break.
    for t in tests:
        try:
            t()
        except AssertionError as e:
            check(t.__name__, False, str(e))
        else:
            check(t.__name__, True)

    # ── Wire-level assertions requiring the orchestrator's rebuilt native .so ──
    # These cannot run here (they need server.zig compiled into the extension):
    #   * HEAD request → identical status line + headers + Content-Length but
    #     ZERO body bytes on the wire (sendResponse/sendFullResponse/static/
    #     serveFile all read the _req_is_head threadlocal).
    #   * A Django response with its own Content-Length → exactly ONE
    #     Content-Length header on the wire (handler.py strips it → Zig adds one).
    #   * 1xx/204/304 responses → no body and no Content-Length on the wire.
    #   * CORS preflight 204 → no malformed empty "Content-Type:" line.
    #   * sendFullResponse now emits a Connection header (HTTP/1.0 keep-alive echo
    #     / close advertised).
    #   * CR/LF in handler content_type / a blank line in the header block are
    #     stripped (no response splitting).
    print(
        "\nNOTE: wire-level HEAD/204/304/Connection/injection assertions require "
        "the rebuilt native module (orchestrator)."
    )
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
