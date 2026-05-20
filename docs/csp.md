# Content Security Policy

Content Security Policy (CSP) is an HTTP response header that controls which resources the browser is allowed to load for a page. It is the most effective defense against Cross-Site Scripting (XSS) attacks, preventing injected scripts from executing even if an attacker finds an injection point.

HyperDjango configures CSP through the `SecurityHeadersMiddleware`.

## Quick Start

```python
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

app.use(
    SecurityHeadersMiddleware(
        csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"
    )
)
```

This sets the `Content-Security-Policy` header on every response, restricting all resource loading to the same origin.

## SecurityHeadersMiddleware

The `SecurityHeadersMiddleware` is a dataclass that adds security headers to all responses. CSP is one of several headers it manages.

```python
from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

security = SecurityHeadersMiddleware(
    content_type_nosniff=True,  # X-Content-Type-Options: nosniff
    frame_options="DENY",  # X-Frame-Options: DENY
    hsts=False,  # Strict-Transport-Security (off by default)
    hsts_max_age=31536000,  # HSTS max-age in seconds (1 year)
    csp=None,  # Content-Security-Policy (off by default)
    referrer_policy="strict-origin-when-cross-origin",
    permissions_policy=None,  # Permissions-Policy header
    cross_origin_opener_policy="same-origin",  # COOP header
)
app.use(security)
```

When `csp` is `None` (the default), no CSP header is sent. Set it to a policy string to enable.

The middleware uses `setdefault` on response headers, so per-response headers set by your handlers take priority over the middleware defaults.

## CSP Directives

A CSP policy is a semicolon-separated list of directives. Each directive controls a specific resource type.

### Common Directives

| Directive         | Controls                                              |
| ----------------- | ----------------------------------------------------- |
| `default-src`     | Fallback for all resource types not explicitly listed |
| `script-src`      | JavaScript (inline and external)                      |
| `style-src`       | CSS (inline and external)                             |
| `img-src`         | Images                                                |
| `font-src`        | Web fonts                                             |
| `connect-src`     | XHR, WebSocket, EventSource, fetch()                  |
| `media-src`       | Audio and video                                       |
| `object-src`      | Plugins (Flash, Java) -- set to `'none'`              |
| `frame-src`       | Iframes                                               |
| `frame-ancestors` | What can embed this page in an iframe                 |
| `base-uri`        | Restricts `<base>` element URLs                       |
| `form-action`     | Restricts form submission targets                     |

### Source Values

| Value                     | Meaning                                                 |
| ------------------------- | ------------------------------------------------------- |
| `'self'`                  | Same origin only                                        |
| `'none'`                  | Block all                                               |
| `'unsafe-inline'`         | Allow inline scripts/styles (weakens CSP significantly) |
| `'unsafe-eval'`           | Allow `eval()` and similar (avoid if possible)          |
| `'nonce-<base64>'`        | Allow specific inline scripts by nonce                  |
| `'strict-dynamic'`        | Trust scripts loaded by already-trusted scripts         |
| `https:`                  | Any HTTPS origin                                        |
| `data:`                   | Data URIs                                               |
| `*.example.com`           | Wildcard subdomain match                                |
| `https://cdn.example.com` | Specific origin                                         |

## Example Policies

### Strict Policy (Recommended Baseline)

```python
app.use(
    SecurityHeadersMiddleware(
        csp=(
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
    )
)
```

### API-Only Application

APIs serving JSON do not need to load any browser resources:

```python
app.use(SecurityHeadersMiddleware(csp="default-src 'none'; frame-ancestors 'none'"))
```

### Application with CDN Assets

```python
app.use(
    SecurityHeadersMiddleware(
        csp=(
            "default-src 'self'; "
            "script-src 'self' https://cdn.example.com; "
            "style-src 'self' https://cdn.example.com 'unsafe-inline'; "
            "img-src 'self' https://cdn.example.com https://images.example.com; "
            "font-src 'self' https://cdn.example.com; "
            "connect-src 'self' https://api.example.com; "
            "object-src 'none'"
        )
    )
)
```

### Application with Inline Scripts (Nonce-Based)

Instead of `'unsafe-inline'`, use nonces to allow specific inline scripts:

```python
import secrets


@app.route("GET", "/page")
async def page(request):
    nonce = secrets.token_urlsafe(16)
    html = f"""
    <html>
    <head>
        <script nonce="{nonce}">
            console.log("This is allowed");
        </script>
    </head>
    <body>Hello</body>
    </html>
    """
    return Response.html(
        html,
        headers={
            "Content-Security-Policy": f"default-src 'self'; script-src 'nonce-{nonce}'"
        },
    )
```

Per-response CSP headers override the middleware default, so you can set a nonce-specific policy on individual pages.

## HyperAdmin Under a Nonce-Based CSP

`HyperAdmin` ships a handful of explicit inline `<script>` and `<style>` blocks (the admin stylesheet, the htmx script tag, the FK-autocomplete helper, and the dark-mode theme toggle). Under a strict `script-src 'nonce-...'` / `style-src 'nonce-...'` policy — i.e. without `'unsafe-inline'` — those blocks must carry a matching `nonce` attribute or the browser will refuse to run them.

The admin templates render the nonce through a request-scoped mechanism, so no admin changes are required beyond supplying the nonce.

### Template helper: `csp_nonce_attr`

`hyperdjango.templating` registers a `csp_nonce_attr` global on every `TemplateEngine`. It returns a space-prefixed attribute fragment so the no-nonce output is byte-identical to a bare tag:

```python
from hyperdjango.templating import csp_nonce_attr

csp_nonce_attr("aB9-_xZ")  # -> ' nonce="aB9-_xZ"'
csp_nonce_attr(None)  # -> ''  (also for "" / missing)
```

Templates invoke it on the explicit inline blocks:

```html
<style{{ csp_nonce_attr(csp_nonce)|safe }}> ... </style>
<script src="/static/htmx.min.js"{{ csp_nonce_attr(csp_nonce)|safe }}></script>
<script{{ csp_nonce_attr(csp_nonce)|safe }}> ... </script>
```

When `csp_nonce` is absent from the context the fragment is empty, so the rendered markup is unchanged (`<style>`, `<script>`). When it is present, each block gets `nonce="<value>"`. The value is sanitized to the base64 nonce character set, so a hostile value can never break out of the quoted attribute.

> The helper deliberately covers **only** the explicit `<script>`/`<style>` blocks. The ~230 inline `style="..."` attributes on admin elements are not nonced — for those you need `style-src 'unsafe-inline'` (or to refactor them into the stylesheet). Use a policy such as `style-src 'self' 'nonce-...' 'unsafe-inline'; script-src 'self' 'nonce-...'`.

### Supplying `csp_nonce`

`HyperAdmin._base_context(request)` reads an optional, middleware-supplied `request.csp_nonce` attribute and surfaces it into the template context. Generate one nonce per request in a middleware, expose it on the request, and reference it in your per-request CSP header:

```python
import secrets


class CSPNonceMiddleware:
    async def __call__(self, request, call_next):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{request.csp_nonce}'; "
            f"style-src 'self' 'nonce-{request.csp_nonce}' 'unsafe-inline'",
        )
        return response


app.use(CSPNonceMiddleware())
```

With the nonce on both the request and the response header, the admin's inline blocks execute under the strict policy while injected inline scripts (which cannot guess the per-request nonce) are blocked.

## Report-Only Mode

To test a CSP policy without enforcing it, use the `Content-Security-Policy-Report-Only` header. Violations are reported but not blocked:

```python
@app.route("GET", "/test-page")
async def test_page(request):
    return Response.html(
        html,
        headers={
            "Content-Security-Policy-Report-Only": (
                "default-src 'self'; report-uri /csp-report"
            )
        },
    )


@app.route("POST", "/csp-report")
async def csp_report(request):
    report = await request.json()
    logger.warning("CSP violation", report=report)
    return Response(body=b"", status=204)
```

This lets you audit what would break before enforcing the policy. Once the report shows no unexpected violations, switch to enforcement by using the `csp` parameter on `SecurityHeadersMiddleware`.

## Other Security Headers

`SecurityHeadersMiddleware` also sets these headers by default:

| Header                       | Default                           | Purpose                       |
| ---------------------------- | --------------------------------- | ----------------------------- |
| `X-Content-Type-Options`     | `nosniff`                         | Prevents MIME-type sniffing   |
| `X-Frame-Options`            | `DENY`                            | Prevents clickjacking         |
| `Referrer-Policy`            | `strict-origin-when-cross-origin` | Controls referrer information |
| `Cross-Origin-Opener-Policy` | `same-origin`                     | Isolates browsing context     |

Enable HSTS for HTTPS-only sites:

```python
app.use(
    SecurityHeadersMiddleware(
        hsts=True,
        hsts_max_age=31536000,  # 1 year
        csp="default-src 'self'",
    )
)
```

## See Also

- [Security Guide](security-guide.md) -- comprehensive security configuration
- [CSRF](csrf.md) -- cross-site request forgery protection
- [Middleware](middleware.md) -- middleware stack and ordering
