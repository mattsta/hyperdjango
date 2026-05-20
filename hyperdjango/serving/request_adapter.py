"""
Request adapter — convert between Zig and Django request formats.
"""

import io

from django.http import HttpRequest, QueryDict


def zig_to_django_request(method, path, headers, body, query_string):
    """Convert Zig HTTP request data into a Django HttpRequest.

    Args:
        method: HTTP method string
        path: URL path
        headers: Dict of headers
        body: Request body bytes
        query_string: Query string

    Returns:
        Django HttpRequest instance
    """
    request = HttpRequest()
    request.method = method
    request.path = path
    request.path_info = path
    request.META = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string or "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8000",
        "wsgi.input": io.BytesIO(body or b""),
    }

    for key, value in (headers or {}).items():
        # Drop header names containing '_': the HTTP_<NAME> mapping collapses
        # both 'X-Foo' and 'X_Foo' to 'HTTP_X_FOO', letting an attacker spoof
        # trusted headers (X-Forwarded-For, X-Real-IP) behind some proxies.
        # Mirrors serving/handler.py and request.py from_asgi for consistency.
        if "_" in key:
            continue
        meta_key = "HTTP_" + key.upper().replace("-", "_")
        if key.lower() == "content-type":
            meta_key = "CONTENT_TYPE"
        elif key.lower() == "content-length":
            meta_key = "CONTENT_LENGTH"
        request.META[meta_key] = value

    request.GET = QueryDict(query_string or "")

    if body:
        request._body = body
        content_type = request.META.get("CONTENT_TYPE", "")
        if "application/x-www-form-urlencoded" in content_type:
            request.POST = QueryDict(body)

    return request
