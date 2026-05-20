# CSRF Protection

Cross-Site Request Forgery protection for POST, PUT, PATCH, and DELETE requests.

## What is CSRF

CSRF is an attack where a malicious website tricks a user's browser into making requests to your application using the user's existing session cookies. For example, a hidden form on `evil.com` could submit a POST request to `yourapp.com/transfer-money` -- and because the browser automatically attaches cookies, the request would be authenticated as the victim.

CSRF protection works by requiring a secret token that the malicious site cannot know. Only pages served by your own application have access to the token, so forged cross-origin requests are rejected.

## How It Works

1. **Token generation**: When CSRFMiddleware is active, a cryptographic token is generated per session. The token is derived from the session secret using HMAC-SHA256, so it is unpredictable and tied to the session.
2. **Token delivery**: The token is rendered into your page as the template variable `{{ csrf_token }}` — use it for a hidden form field AND for JavaScript (e.g. `<meta name="csrf-token" content="{{ csrf_token }}">`, then read it in JS). A `csrf_token` cookie is also set for the double-submit comparison, but it is **HttpOnly by default** (`CSRF_COOKIE_HTTPONLY=True`), so JavaScript cannot read it — get the token from the rendered template variable, not from the cookie. (Set `CSRF_COOKIE_HTTPONLY=False` only if you specifically need JS to read the cookie.)
3. **Token submission**: On form submission, the token is sent back as either a hidden form field (`csrf_token`) or an HTTP header (`X-CSRF-Token`).
4. **Validation**: The middleware compares the submitted token against the expected value. If they do not match (or if no token is submitted), the request is rejected with HTTP 403 Forbidden.

## Setup

```python
from hyperdjango.standalone_middleware import CSRFMiddleware

app.use(CSRFMiddleware(secret="your-secret-key"))
```

### CSRFMiddleware parameters

| Parameter      | Type        | Default    | Description                                                |
| -------------- | ----------- | ---------- | ---------------------------------------------------------- |
| `secret`       | `str`       | (required) | Secret key used for token generation. Must be kept secret. |
| `exempt_paths` | `list[str]` | `[]`       | URL paths that skip CSRF validation                        |

The `secret` should be a long, random string. In production, load it from an environment variable:

```python
import os

app.use(
    CSRFMiddleware(
        secret=os.environ["SECRET_KEY"],
        exempt_paths=["/api/webhook", "/api/stripe-hook"],
    )
)
```

## Safe Methods

CSRF validation only applies to state-changing HTTP methods:

| Method    | CSRF Required |
| --------- | ------------- |
| `GET`     | No            |
| `HEAD`    | No            |
| `OPTIONS` | No            |
| `TRACE`   | No            |
| `POST`    | **Yes**       |
| `PUT`     | **Yes**       |
| `PATCH`   | **Yes**       |
| `DELETE`  | **Yes**       |

GET, HEAD, OPTIONS, and TRACE are considered "safe" methods because they should not cause side effects. These requests pass through the middleware without any token check.

## In Templates

Include the CSRF token in every form that uses POST (or PUT/PATCH/DELETE):

```html
<form method="post" action="/submit">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input type="text" name="message" />
  <button type="submit">Send</button>
</form>
```

The `csrf_token` variable is automatically available in the template context when CSRFMiddleware is active. You do not need to pass it manually from your view.

### Multiple forms on one page

Each form needs its own hidden input, but they all use the same token value:

```html
<form method="post" action="/create">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input type="text" name="title" />
  <button type="submit">Create</button>
</form>

<form method="post" action="/delete/{{ item.id }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <button type="submit">Delete</button>
</form>
```

## For AJAX/JavaScript Requests

For JavaScript-initiated requests (fetch, XMLHttpRequest), send the token in the `X-CSRF-Token` header instead of a form field.

### Reading the token

The token is available from a cookie or a meta tag. The meta tag approach is recommended:

```html
<!-- In your base template <head> -->
<meta name="csrf-token" content="{{ csrf_token }}" />
```

```javascript
// Read token from meta tag
const token = document.querySelector('meta[name="csrf-token"]').content;

fetch("/api/items", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": token,
  },
  body: JSON.stringify({ name: "New Item" }),
});
```

### Reading from cookie

Alternatively, read the token from the `csrf_token` cookie:

```javascript
function getCsrfToken() {
  const match = document.cookie.match(/csrf_token=([^;]+)/);
  return match ? match[1] : null;
}

fetch("/api/items", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": getCsrfToken(),
  },
  body: JSON.stringify({ name: "New Item" }),
});
```

### Global fetch wrapper

To avoid repeating the header on every request, create a wrapper:

```javascript
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

async function apiFetch(url, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrfToken,
    ...options.headers,
  };
  return fetch(url, { ...options, headers });
}

// Usage
await apiFetch("/api/items", {
  method: "POST",
  body: JSON.stringify({ name: "New Item" }),
});
```

## Exempt Paths

For endpoints that use their own authentication mechanism (API keys, webhook signatures, OAuth2 tokens), CSRF protection is unnecessary and should be skipped. These endpoints do not rely on browser cookies for authentication, so CSRF attacks do not apply.

```python
app.use(
    CSRFMiddleware(
        secret="your-secret-key",
        exempt_paths=[
            "/api/webhook",
            "/api/stripe-hook",
            "/api/github-hook",
        ],
    )
)
```

### When to exempt paths

Exempt a path when:

- The endpoint uses API key authentication (the key is not sent automatically by the browser)
- The endpoint verifies a webhook signature (e.g., Stripe, GitHub)
- The endpoint uses OAuth2 Bearer tokens

Do NOT exempt a path when:

- The endpoint uses cookie-based session authentication
- The endpoint is called by forms in your own HTML pages

## Token Validation Flow

The middleware checks for the token in this order:

1. **Form field**: Looks for `csrf_token` in the POST body form data
2. **Header**: Looks for `X-CSRF-Token` in the request headers
3. **Cookie comparison**: The submitted token is compared against the expected token derived from the session

If neither the form field nor the header contains a valid token, the request is rejected with HTTP 403 Forbidden.

### The 403 response

When CSRF validation fails, the response body contains:

```json
{ "error": "CSRF token missing or invalid" }
```

The response includes a `Content-Type: application/json` header so JavaScript clients can parse it.

## CSRF for Forms vs APIs

| Scenario                   | Authentication         | CSRF Needed | Approach                  |
| -------------------------- | ---------------------- | ----------- | ------------------------- |
| HTML form                  | Session cookie         | **Yes**     | Hidden `csrf_token` field |
| AJAX from same-origin page | Session cookie         | **Yes**     | `X-CSRF-Token` header     |
| API with API key           | API key header         | No          | Exempt the path           |
| Webhook                    | Signature verification | No          | Exempt the path           |
| OAuth2 API                 | Bearer token           | No          | Exempt the path           |

The rule: if the request is authenticated via cookies that the browser sends automatically, you need CSRF. If the request requires a manually-attached credential (API key, Bearer token), you do not.

## SameSite Cookie Protection

In addition to CSRF tokens, set `SameSite=Lax` on your session cookies for defense in depth. This tells the browser not to send the cookie on cross-origin POST requests:

```python
app.use(
    SessionAuth(
        secret="your-secret-key",
        cookie_samesite="Lax",  # Prevents cross-origin POST with cookies
        cookie_secure=True,  # HTTPS only
        cookie_httponly=True,  # Not accessible via JavaScript
    )
)
```

`SameSite=Lax` allows the cookie to be sent on top-level GET navigations (clicking a link to your site) but blocks it on cross-origin form submissions and AJAX requests. This stops most CSRF attacks at the browser level, before the token check even runs.

**Important**: SameSite is a defense-in-depth measure, not a replacement for CSRF tokens. Older browsers may not support it, and there are edge cases (e.g., same-site subdomains) where it does not fully protect.

## Testing with CSRF

The `TestClient` has CSRF handling built in. By default, it does NOT enforce CSRF checks (to keep tests simple):

```python
from hyperdjango.testing import TestClient


async def test_create_item():
    async with TestClient(app) as client:
        # No CSRF token needed in tests by default
        response = await client.post("/items", json={"name": "Widget"})
        assert response.status_code == 201
```

### Enabling CSRF enforcement in tests

To test that your CSRF protection works correctly, enable enforcement:

```python
async def test_csrf_enforced():
    async with TestClient(app, enforce_csrf_checks=True) as client:
        # Without token — should be rejected
        response = await client.post("/items", json={"name": "Widget"})
        assert response.status_code == 403

        # With token — should succeed
        # First, get a page to obtain the token
        page = await client.get("/items/new")
        token = extract_csrf_token(page.text)

        response = await client.post(
            "/items",
            data={"name": "Widget", "csrf_token": token},
        )
        assert response.status_code == 201
```

### Testing AJAX with CSRF

```python
async def test_ajax_csrf():
    async with TestClient(app, enforce_csrf_checks=True) as client:
        # Get the token from a page or cookie
        await client.get("/")
        token = client.cookies.get("csrf_token")

        response = await client.post(
            "/api/items",
            json={"name": "Widget"},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 201
```

## Common CSRF Errors and Solutions

### "CSRF token missing or invalid" on form submission

**Cause**: The hidden `csrf_token` input is missing from the form.

**Fix**: Add the hidden field:

```html
<input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
```

### "CSRF token missing or invalid" on AJAX request

**Cause**: The `X-CSRF-Token` header is not being sent.

**Fix**: Add the header to your fetch call:

```javascript
headers: { "X-CSRF-Token": token }
```

### CSRF errors after deploying a new version

**Cause**: The `secret` key changed between deployments, invalidating all existing tokens.

**Fix**: Keep the `secret` stable across deployments. Store it in an environment variable, not in code.

### CSRF errors in iframe or cross-origin context

**Cause**: The browser is blocking the cookie due to SameSite policy.

**Fix**: If you legitimately need cross-origin form submission (rare), you may need to adjust the SameSite setting. In most cases, cross-origin form submission is exactly what CSRF protection is designed to block -- verify this is not an actual attack.

### CSRF errors with file uploads

**Cause**: Multipart form data must include the `csrf_token` field like any other form field.

**Fix**: The hidden input works the same way in multipart forms:

```html
<form method="post" action="/upload" enctype="multipart/form-data">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}" />
  <input type="file" name="document" />
  <button type="submit">Upload</button>
</form>
```

## Security Notes

- Always use HTTPS in production -- CSRF tokens can be intercepted over HTTP
- The `secret` parameter must be kept confidential. If it leaks, an attacker can forge valid tokens.
- API endpoints using Bearer tokens or API keys do not need CSRF -- the token itself proves the request is intentional
- CSRF protects against attacks where a malicious site triggers requests using the user's browser cookies
- Rotate your secret key if you suspect it has been compromised. This will invalidate all active sessions and tokens.
