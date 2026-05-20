# Email

Send emails via SMTP, console output, or in-memory backend for testing. Supports plain text, HTML with text fallback, CC/BCC, reply-to, custom headers, and bulk sending.

## Quick Start

```python
from hyperdjango.mail import send_mail, configure_mail

# Configure once at startup
configure_mail(
    host="smtp.gmail.com",
    port=587,
    username="you@gmail.com",
    password="app-password",
    use_tls=True,
    default_from="you@gmail.com",
)

# Send a simple email
await send_mail(
    subject="Welcome!",
    body="Thanks for signing up.",
    recipients=["user@example.com"],
)
```

## Configuration

### `configure_mail()`

Configure the global email settings. Call once at application startup:

```python
from hyperdjango.mail import configure_mail

configure_mail(
    host="smtp.gmail.com",
    port=587,
    username="user@gmail.com",
    password="app-password",
    use_tls=True,
    use_ssl=False,
    default_from="noreply@myapp.com",
    backend="smtp",
    timeout=30,
)
```

### Parameters

| Parameter      | Type   | Default               | Description                                         |
| -------------- | ------ | --------------------- | --------------------------------------------------- |
| `host`         | `str`  | `"localhost"`         | SMTP server hostname                                |
| `port`         | `int`  | `587`                 | SMTP server port                                    |
| `username`     | `str`  | `""`                  | SMTP authentication username                        |
| `password`     | `str`  | `""`                  | SMTP authentication password                        |
| `use_tls`      | `bool` | `True`                | Use STARTTLS after connecting (port 587)            |
| `use_ssl`      | `bool` | `False`               | Use implicit SSL connection (port 465)              |
| `default_from` | `str`  | `"noreply@localhost"` | Default sender address when `from_email` is omitted |
| `backend`      | `str`  | `"smtp"`              | Backend type: `"smtp"`, `"console"`, or `"memory"`  |
| `timeout`      | `int`  | `30`                  | Connection timeout in seconds                       |

### `get_mail_config()`

Retrieve the current configuration:

```python
from hyperdjango.mail import get_mail_config

config = get_mail_config()
print(config.host)  # "smtp.gmail.com"
print(config.backend)  # "smtp"
```

Returns a `MailConfig` dataclass with all the fields above.

### TLS vs SSL

- **`use_tls=True`** (default): Connect to port 587 in plaintext, then upgrade to TLS via `STARTTLS`. This is the most common configuration for modern SMTP servers.
- **`use_ssl=True`**: Connect to port 465 with implicit SSL from the start. Used by some older servers.
- Do not set both to `True`. If `use_ssl=True`, the TLS upgrade step is skipped since the connection is already encrypted.

### Common Provider Configurations

**Gmail (App Password)**:

```python
configure_mail(
    host="smtp.gmail.com",
    port=587,
    username="you@gmail.com",
    password="xxxx-xxxx-xxxx-xxxx",  # App password, not account password
    use_tls=True,
)
```

**Amazon SES**:

```python
configure_mail(
    host="email-smtp.us-east-1.amazonaws.com",
    port=587,
    username="AKIAIOSFODNN7EXAMPLE",
    password="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    use_tls=True,
    default_from="noreply@yourdomain.com",
)
```

**Mailgun**:

```python
configure_mail(
    host="smtp.mailgun.org",
    port=587,
    username="postmaster@yourdomain.com",
    password="your-mailgun-password",
    use_tls=True,
)
```

**SendGrid**:

```python
configure_mail(
    host="smtp.sendgrid.net",
    port=587,
    username="apikey",
    password="SG.your-api-key",
    use_tls=True,
)
```

## Backends

### SMTP Backend (Production)

The default backend. Sends real emails via SMTP:

```python
configure_mail(backend="smtp", host="smtp.gmail.com", ...)
```

Connection flow:

1. If `use_ssl`: connect with `SMTP_SSL` (implicit SSL)
2. Else: connect with `SMTP`, then `STARTTLS` if `use_tls`
3. Login with `username`/`password` if provided
4. `sendmail()` to all recipients (To + CC + BCC)
5. `quit()` to close connection

### Console Backend (Development)

Prints emails to stdout instead of sending. Useful for development:

```python
configure_mail(backend="console")

await send_mail(
    subject="Test",
    body="Hello",
    recipients=["user@example.com"],
)
```

Output:

```
============================================================
EMAIL: Test
From: noreply@localhost
To: user@example.com
============================================================
Hello
============================================================
```

HTML body is printed separately if present:

```
--- HTML ---
<h1>Hello</h1>
```

### Memory Backend (Testing)

Stores sent emails in an in-memory list. Perfect for automated tests:

```python
configure_mail(backend="memory")

await send_mail(
    subject="Test",
    body="Hello",
    recipients=["user@example.com"],
)

# Access sent messages
from hyperdjango.mail import get_outbox, clear_outbox

outbox = get_outbox()
assert len(outbox) == 1
assert outbox[0].subject == "Test"
assert "user@example.com" in outbox[0].recipients

# Clear between tests
clear_outbox()
```

The outbox is thread-safe (protected by a lock).

## `send_mail()`

Convenience function for sending a single email:

```python
from hyperdjango.mail import send_mail

success = await send_mail(
    subject="Order Confirmation",
    body="Your order #123 has been confirmed.",
    recipients=["customer@example.com"],
    from_email="orders@myapp.com",
    html_body="<h1>Order Confirmed</h1><p>Order #123</p>",
    cc=["accounting@myapp.com"],
    bcc=["archive@myapp.com"],
)
```

### Parameters

| Parameter    | Type                | Default  | Description                                                  |
| ------------ | ------------------- | -------- | ------------------------------------------------------------ |
| `subject`    | `str`               | required | Email subject line                                           |
| `body`       | `str`               | required | Plain text body                                              |
| `recipients` | `list[str]`         | required | List of recipient email addresses (To field)                 |
| `from_email` | `str`               | `""`     | Sender address. Falls back to `default_from` from config.    |
| `html_body`  | `str`               | `""`     | Optional HTML body. Creates `multipart/alternative` message. |
| `cc`         | `list[str] \| None` | `None`   | CC recipients                                                |
| `bcc`        | `list[str] \| None` | `None`   | BCC recipients (not visible in headers)                      |

Returns `True` on success, `False` on failure. Uses the globally configured backend.

## `EmailMessage`

For full control over email construction:

```python
from hyperdjango.mail import EmailMessage

msg = EmailMessage(
    subject="Invoice #456",
    body="Please find your invoice attached.",
    recipients=["customer@example.com"],
    from_email="billing@myapp.com",
    cc=["accounting@myapp.com"],
    bcc=["archive@myapp.com"],
    reply_to="support@myapp.com",
    headers={"X-Priority": "1", "X-Mailer": "HyperDjango"},
)

success = await msg.send()
```

### Fields

| Field        | Type             | Default  | Description                                   |
| ------------ | ---------------- | -------- | --------------------------------------------- |
| `subject`    | `str`            | required | Subject line                                  |
| `body`       | `str`            | required | Plain text body                               |
| `recipients` | `list[str]`      | required | To addresses                                  |
| `from_email` | `str`            | `""`     | Sender address (falls back to config default) |
| `html_body`  | `str`            | `""`     | HTML body (creates multipart/alternative)     |
| `cc`         | `list[str]`      | `[]`     | CC addresses                                  |
| `bcc`        | `list[str]`      | `[]`     | BCC addresses (sent to but not in headers)    |
| `reply_to`   | `str`            | `""`     | Reply-To address                              |
| `headers`    | `dict[str, str]` | `{}`     | Extra MIME headers                            |

### `send()`

Send the email using the globally configured backend. Returns `True` on success.

```python
success = await msg.send()
```

### MIME Construction

The `EmailMessage` builds the MIME message automatically:

- **Plain text only** (`html_body` empty): Single `MIMEText("plain")` message
- **HTML + plain text** (`html_body` set): `MIMEMultipart("alternative")` with both `text/plain` and `text/html` parts. Email clients display whichever they prefer (HTML-capable clients show HTML, others show plain text).

Headers set automatically:

- `Subject`
- `From`
- `To` (comma-separated recipients)
- `Cc` (if cc provided)
- `Reply-To` (if reply_to provided)
- Custom headers from `headers` dict

BCC recipients are included in the `sendmail` call but not in the message headers.

## HTML Email

### Inline HTML

```python
msg = EmailMessage(
    subject="Newsletter",
    body="This week's news in plain text.",
    html_body="""
    <html>
    <body>
        <h1>This Week's News</h1>
        <p>Here are the highlights...</p>
    </body>
    </html>
    """,
    recipients=["subscriber@example.com"],
    from_email="news@myapp.com",
)
await msg.send()
```

### With Template Rendering

```python
from hyperdjango.templating import render_template

html = render_template(
    "emails/welcome.html",
    {
        "user_name": "Alice",
        "activation_url": "https://myapp.com/activate/abc123",
    },
)

text = render_template(
    "emails/welcome.txt",
    {
        "user_name": "Alice",
        "activation_url": "https://myapp.com/activate/abc123",
    },
)

msg = EmailMessage(
    subject="Welcome to MyApp!",
    body=text,
    html_body=html,
    recipients=["alice@example.com"],
    from_email="welcome@myapp.com",
)
await msg.send()
```

## Bulk Email

### `send_mass_mail()`

Send multiple emails over a single SMTP connection (more efficient than calling `send_mail` repeatedly):

```python
from hyperdjango.mail import send_mass_mail

messages = [
    ("Subject 1", "Body 1", "from@example.com", ["to1@example.com"]),
    ("Subject 2", "Body 2", "from@example.com", ["to2@example.com"]),
    ("Subject 3", "Body 3", "from@example.com", ["to3@example.com"]),
]

await send_mass_mail(messages)
```

Each element is a tuple of `(subject, body, from_email, recipients)`. A single SMTP connection is used for all messages.

### Manual Bulk Sending

For more control (HTML, CC, headers), create `EmailMessage` instances and send them:

```python
from hyperdjango.mail import EmailMessage

messages = []
for user in users:
    msg = EmailMessage(
        subject="Monthly Report",
        body=f"Hi {user.name}, here is your report.",
        html_body=render_template("emails/report.html", {"user": user}),
        recipients=[user.email],
        from_email="reports@myapp.com",
        headers={"List-Unsubscribe": f"<mailto:unsub+{user.id}@myapp.com>"},
    )
    messages.append(msg)

for msg in messages:
    await msg.send()
```

## Password Reset Email

Built-in password reset flow with HMAC tokens:

```python
from hyperdjango.auth.password_reset import send_password_reset_email

await send_password_reset_email(
    user=user,
    from_email="noreply@myapp.com",
    subject="Reset Your Password",
    reset_url_template="https://myapp.com/reset/{token}/",
)
```

This generates a time-limited HMAC token, constructs the reset URL, and sends an email. The token is validated when the user clicks the link.

### Custom Reset Email

For custom styling or content:

```python
from hyperdjango.auth.password_reset import generate_reset_token

token = generate_reset_token(user)
reset_url = f"https://myapp.com/reset/{token}/"

msg = EmailMessage(
    subject="Reset Your Password",
    body=f"Click here to reset: {reset_url}",
    html_body=render_template(
        "emails/reset.html",
        {
            "user": user,
            "reset_url": reset_url,
        },
    ),
    recipients=[user.email],
    from_email="noreply@myapp.com",
)
await msg.send()
```

## Testing

### Memory Backend

Use the memory backend to capture sent emails in tests:

```python
from hyperdjango.mail import configure_mail, send_mail, get_outbox, clear_outbox

# Setup
configure_mail(backend="memory")


async def test_welcome_email():
    clear_outbox()

    await send_mail(
        subject="Welcome!",
        body="Hello",
        recipients=["user@test.com"],
    )

    outbox = get_outbox()
    assert len(outbox) == 1

    msg = outbox[0]
    assert msg.subject == "Welcome!"
    assert msg.body == "Hello"
    assert "user@test.com" in msg.recipients
    assert msg.from_email == "" or msg.from_email  # uses default_from
```

### Testing HTML Content

```python
async def test_html_email():
    clear_outbox()

    msg = EmailMessage(
        subject="Invoice",
        body="Your invoice.",
        html_body="<h1>Invoice</h1>",
        recipients=["user@test.com"],
        cc=["copy@test.com"],
    )
    await msg.send()

    outbox = get_outbox()
    assert len(outbox) == 1
    assert outbox[0].html_body == "<h1>Invoice</h1>"
    assert outbox[0].cc == ["copy@test.com"]
```

### Testing Password Reset

```python
async def test_password_reset():
    clear_outbox()

    await send_password_reset_email(
        user=user,
        from_email="noreply@test.com",
        subject="Reset",
        reset_url_template="https://test.com/reset/{token}/",
    )

    outbox = get_outbox()
    assert len(outbox) == 1
    assert "reset" in outbox[0].body.lower()
```

## Custom Headers

Add any MIME headers to the email:

```python
msg = EmailMessage(
    subject="Order #123",
    body="Your order is confirmed.",
    recipients=["customer@example.com"],
    headers={
        "X-Priority": "1",
        "X-Mailer": "MyApp/1.0",
        "Message-ID": "<order-123@myapp.com>",
        "List-Unsubscribe": "<mailto:unsub@myapp.com>",
        "References": "<original-msg-id@myapp.com>",
    },
)
await msg.send()
```

Common headers:

| Header             | Description                                           |
| ------------------ | ----------------------------------------------------- |
| `X-Priority`       | Email priority (1=high, 3=normal, 5=low)              |
| `X-Mailer`         | Identify your sending application                     |
| `Message-ID`       | Unique message identifier                             |
| `In-Reply-To`      | Message ID of the email being replied to              |
| `References`       | Chain of message IDs for threading                    |
| `List-Unsubscribe` | One-click unsubscribe support (required by some ESPs) |

## Complete Example

```python
from hyperdjango import HyperApp
from hyperdjango.mail import configure_mail, send_mail, EmailMessage

app = HyperApp("myapp")

# Configure at startup
configure_mail(
    host="smtp.gmail.com",
    port=587,
    username="noreply@myapp.com",
    password="app-password",
    use_tls=True,
    default_from="MyApp <noreply@myapp.com>",
)


@app.post("/contact")
async def contact(request):
    data = request.json
    msg = EmailMessage(
        subject=f"Contact: {data['subject']}",
        body=data["message"],
        recipients=["support@myapp.com"],
        from_email=data["email"],
        reply_to=data["email"],
        headers={"X-Contact-Form": "true"},
    )
    success = await msg.send()

    if success:
        return Response.json({"status": "sent"})
    return Response.json({"status": "failed"}, status=500)


@app.post("/newsletter/subscribe")
async def subscribe(request):
    data = request.json
    await send_mail(
        subject="Welcome to our newsletter!",
        body="You're now subscribed.",
        html_body="<h1>Welcome!</h1><p>You're now subscribed to our newsletter.</p>",
        recipients=[data["email"]],
    )
    return Response.json({"status": "subscribed"})
```
