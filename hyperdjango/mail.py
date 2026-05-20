"""
Email sending for HyperApp.

SMTP-based email backend using Python's smtplib. Supports plain text and HTML,
TLS/SSL, and configurable per-app settings.

Usage:
    from hyperdjango.mail import send_mail, EmailMessage, configure_mail

    # Configure (once, at startup)
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
        body="Hello, welcome to our app.",
        recipients=["user@example.com"],
    )

    # Send HTML email
    msg = EmailMessage(
        subject="Order Confirmation",
        body="Your order #123 is confirmed.",
        html_body="<h1>Order #123</h1><p>Confirmed!</p>",
        from_email="orders@example.com",
        recipients=["customer@example.com"],
    )
    await msg.send()

    # Console backend (for development — prints to stdout)
    configure_mail(backend="console")
"""

import asyncio
import contextlib
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from hyperdjango.conf import get_setting
from hyperdjango.logging import logger


def _reject_header_injection(field_name: str, value: str) -> None:
    """Reject header values containing CR/LF (SMTP header injection).

    An attacker who controls e.g. a subject or recipient could smuggle
    extra headers or body content by embedding ``\\r``/``\\n``. Raise so the
    injection surfaces to the caller instead of being silently sent.
    """
    if "\r" in value or "\n" in value:
        raise ValueError(
            f"Header injection detected in {field_name!r}: "
            "CR/LF characters are not allowed in header values"
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class MailConfig:
    """Email configuration."""

    host: str = "localhost"
    port: int = 25
    username: str = ""
    password: str = ""
    use_tls: bool = False
    use_ssl: bool = False
    default_from: str = "webmaster@localhost"
    backend: str = "smtp"  # "smtp" or "console" or "memory"
    timeout: int = 30


_config = MailConfig(
    host=get_setting("EMAIL_HOST"),
    port=get_setting("EMAIL_PORT"),
    username=get_setting("EMAIL_HOST_USER"),
    password=get_setting("EMAIL_HOST_PASSWORD"),
    use_tls=get_setting("EMAIL_USE_TLS"),
    use_ssl=get_setting("EMAIL_USE_SSL"),
    default_from=get_setting("DEFAULT_FROM_EMAIL"),
    backend=get_setting("EMAIL_BACKEND"),
    timeout=get_setting("EMAIL_TIMEOUT"),
)
_memory_outbox: list[EmailMessage] = []
_outbox_lock = threading.Lock()


def configure_mail(
    *,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool | None = None,
    use_ssl: bool | None = None,
    default_from: str | None = None,
    backend: str | None = None,
    timeout: int | None = None,
):
    """Configure the global email settings.

    Explicit parameters take priority; settings from conf.py are the defaults.
    """
    global _config
    _config = MailConfig(
        host=host if host is not None else get_setting("EMAIL_HOST"),
        port=port if port is not None else get_setting("EMAIL_PORT"),
        username=username if username is not None else get_setting("EMAIL_HOST_USER"),
        password=password
        if password is not None
        else get_setting("EMAIL_HOST_PASSWORD"),
        use_tls=use_tls if use_tls is not None else get_setting("EMAIL_USE_TLS"),
        use_ssl=use_ssl if use_ssl is not None else get_setting("EMAIL_USE_SSL"),
        default_from=default_from
        if default_from is not None
        else get_setting("DEFAULT_FROM_EMAIL"),
        backend=backend if backend is not None else get_setting("EMAIL_BACKEND"),
        timeout=timeout if timeout is not None else get_setting("EMAIL_TIMEOUT"),
    )


def get_mail_config() -> MailConfig:
    """Get the current mail configuration."""
    return _config


def get_outbox() -> list[EmailMessage]:
    """Get sent emails (memory backend only). For testing."""
    with _outbox_lock:
        return list(_memory_outbox)


def clear_outbox():
    """Clear the test outbox."""
    with _outbox_lock:
        _memory_outbox.clear()


# ---------------------------------------------------------------------------
# EmailMessage
# ---------------------------------------------------------------------------


@dataclass
class EmailMessage:
    """An email message with optional HTML body."""

    subject: str
    body: str  # Plain text body
    recipients: list[str]
    from_email: str = ""
    html_body: str = ""  # Optional HTML body
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def _validate_headers(self, from_addr: str) -> None:
        """Reject CR/LF injection across every attacker-influenced header."""
        _reject_header_injection("subject", self.subject)
        _reject_header_injection("from", from_addr)
        _reject_header_injection("reply_to", self.reply_to)
        for addr in self.recipients:
            _reject_header_injection("to", addr)
        for addr in self.cc:
            _reject_header_injection("cc", addr)
        for addr in self.bcc:
            _reject_header_injection("bcc", addr)
        for key, value in self.headers.items():
            _reject_header_injection(key, key)
            _reject_header_injection(key, value)

    async def send(self) -> bool:
        """Send the email. Returns True on success.

        The SMTP path runs in a worker thread (``asyncio.to_thread``) so a
        slow or unresponsive server (up to the configured timeout) never
        blocks the event loop and stalls other in-flight requests.
        """
        config = _config
        # Use SERVER_EMAIL from settings as fallback for system emails
        server_email = get_setting("SERVER_EMAIL")
        from_addr = self.from_email or server_email or config.default_from

        # Apply EMAIL_SUBJECT_PREFIX from settings
        prefix = get_setting("EMAIL_SUBJECT_PREFIX")
        if prefix and not self.subject.startswith(prefix):
            self.subject = prefix + self.subject

        # Guard against SMTP header injection before dispatching to any backend.
        self._validate_headers(from_addr)

        if config.backend == "console":
            return self._send_console(from_addr)
        elif config.backend == "memory":
            return self._send_memory()
        else:
            # smtplib is blocking — offload to a thread so the event loop
            # (and every concurrent request on it) keeps running.
            return await asyncio.to_thread(self._send_smtp, config, from_addr)

    def _build_mime(self, from_addr: str) -> MIMEMultipart | MIMEText:
        """Build the MIME message."""
        if self.html_body:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(self.body, "plain"))
            msg.attach(MIMEText(self.html_body, "html"))
        else:
            msg = MIMEText(self.body, "plain")

        msg["Subject"] = self.subject
        msg["From"] = from_addr
        msg["To"] = ", ".join(self.recipients)
        if self.cc:
            msg["Cc"] = ", ".join(self.cc)
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        for key, value in self.headers.items():
            msg[key] = value

        return msg

    def _send_smtp(self, config: MailConfig, from_addr: str) -> bool:
        """Send via SMTP."""
        msg = self._build_mime(from_addr)
        all_recipients = self.recipients + self.cc + self.bcc

        # Honor config.timeout so configure_mail(timeout=...) takes effect.
        # MailConfig.timeout already defaults to the EMAIL_TIMEOUT setting.
        timeout = config.timeout

        ssl_certfile = get_setting("EMAIL_SSL_CERTFILE") or None
        ssl_keyfile = get_setting("EMAIL_SSL_KEYFILE") or None

        try:
            if config.use_ssl:
                ctx = ssl.create_default_context()
                if ssl_certfile:
                    ctx.load_cert_chain(certfile=ssl_certfile, keyfile=ssl_keyfile)
                server = smtplib.SMTP_SSL(
                    config.host, config.port, timeout=timeout, context=ctx
                )
            else:
                server = smtplib.SMTP(config.host, config.port, timeout=timeout)

            try:
                if config.use_tls and not config.use_ssl:
                    # Always use a verifying context (cert + hostname checks).
                    # Passing context=None makes starttls() build an UNVERIFIED
                    # context, silently exposing the session to MITM. Mirror the
                    # use_ssl path and honor EMAIL_SSL_CERTFILE for client certs.
                    ctx = ssl.create_default_context()
                    if ssl_certfile:
                        ctx.load_cert_chain(certfile=ssl_certfile, keyfile=ssl_keyfile)
                    server.starttls(context=ctx)
                if config.username:
                    server.login(config.username, config.password)
                server.sendmail(from_addr, all_recipients, msg.as_string())
                return True
            finally:
                # quit() can raise SMTPServerDisconnected (e.g. the server
                # already dropped the connection after a send error), which
                # would mask the real exception propagating out of the try.
                # Suppress it so the true send failure surfaces in the except.
                with contextlib.suppress(Exception):
                    server.quit()
        # blind-except: email send returns a success boolean — every SMTP failure (auth, DNS, TLS, timeout) is logged before returning False, per the method's contract
        except Exception as exc:
            # Never swallow failures silently — surface them in the logs so
            # delivery problems (auth, DNS, TLS, timeouts) are diagnosable.
            logger.error(
                f"SMTP send failed via {config.host}:{config.port}: "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    def _send_console(self, from_addr: str) -> bool:
        """Print to stdout (development mode)."""
        logger.opt(raw=True).info(f"\n{'=' * 60}\n")
        logger.opt(raw=True).info(f"EMAIL: {self.subject}\n")
        logger.opt(raw=True).info(f"From: {from_addr}\n")
        logger.opt(raw=True).info(f"To: {', '.join(self.recipients)}\n")
        if self.cc:
            logger.opt(raw=True).info(f"Cc: {', '.join(self.cc)}\n")
        logger.opt(raw=True).info(f"{'=' * 60}\n")
        logger.opt(raw=True).info(f"{self.body}\n")
        if self.html_body:
            logger.opt(raw=True).info(f"\n--- HTML ---\n{self.html_body}\n")
        logger.opt(raw=True).info(f"{'=' * 60}\n\n")
        return True

    def _send_memory(self) -> bool:
        """Store in memory (testing mode)."""
        with _outbox_lock:
            _memory_outbox.append(self)
        return True


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


async def send_mail(
    subject: str,
    body: str,
    recipients: list[str],
    from_email: str = "",
    html_body: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> bool:
    """Send an email. Returns True on success.

    Uses the globally configured mail backend.
    """
    msg = EmailMessage(
        subject=subject,
        body=body,
        recipients=recipients,
        from_email=from_email,
        html_body=html_body,
        cc=cc or [],
        bcc=bcc or [],
    )
    return await msg.send()


async def mail_admins(
    subject: str,
    body: str,
    from_email: str = "",
    html_body: str = "",
) -> bool:
    """Send an email to all ADMINS from conf settings.

    ADMINS is a list of (name, email) tuples. The email addresses are
    extracted and used as recipients. Uses SERVER_EMAIL as the default
    sender address.

    Returns True if sent successfully, False if no admins configured or send failed.
    """
    admins = get_setting("ADMINS")
    if not admins:
        return False
    recipients = [email for _name, email in admins]
    server_email = from_email or get_setting("SERVER_EMAIL")
    return await send_mail(
        subject=subject,
        body=body,
        recipients=recipients,
        from_email=server_email,
        html_body=html_body,
    )


async def mail_managers(
    subject: str,
    body: str,
    from_email: str = "",
    html_body: str = "",
) -> bool:
    """Send an email to all MANAGERS from conf settings.

    MANAGERS is a list of (name, email) tuples. Uses SERVER_EMAIL as the
    default sender address.

    Returns True if sent successfully, False if no managers configured or send failed.
    """
    managers = get_setting("MANAGERS")
    if not managers:
        return False
    recipients = [email for _name, email in managers]
    server_email = from_email or get_setting("SERVER_EMAIL")
    return await send_mail(
        subject=subject,
        body=body,
        recipients=recipients,
        from_email=server_email,
        html_body=html_body,
    )
