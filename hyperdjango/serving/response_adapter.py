"""
Response adapter — convert Django HttpResponse to Zig response format.
"""


def django_response_to_zig(response):
    """Convert a BUFFERED Django HttpResponse to a tuple for the Zig HTTP server.

    NOTE: This adapter materializes the whole body and is intended ONLY for
    fully-buffered responses. It is NOT on the serving hot path — the live Django
    bridge is ``hyperdjango.serving.handler.ZigHandler``, which drives
    StreamingHttpResponse/FileResponse INCREMENTALLY through the native
    chunked-send path (``_make_stream_pull``) rather than joining an unbounded
    ``streaming_content`` generator into memory. Callers with a streaming response
    must use that path, not this adapter.

    Args:
        response: Django HttpResponse instance (buffered).

    Returns:
        (status_code, headers_dict, body_bytes) tuple
    """
    headers = {}
    for key, value in response.items():
        headers[key] = value

    body = b""
    if hasattr(response, "content"):
        body = response.content
    elif hasattr(response, "streaming_content"):
        # Buffered fallback: prefer ZigHandler's incremental streaming path for
        # StreamingHttpResponse/FileResponse (this adapter is off the hot path).
        body = b"".join(response.streaming_content)

    return (response.status_code, headers, body)
