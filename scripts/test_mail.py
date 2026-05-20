#!/usr/bin/env python3
"""
Tests for the email sending backend (hyperdjango.mail).

Covers: console/memory backends, SMTP header-injection rejection, timeout
honoring (configure_mail(timeout=) reaches the SMTP client), and that the
blocking SMTP call is offloaded off the event loop.

Usage:
    uv run hyper-test mail
"""

# hyper-test: unit

import asyncio
import sys

from hyperdjango import mail as mail_mod
from hyperdjango.mail import (
    EmailMessage,
    clear_outbox,
    configure_mail,
    get_mail_config,
    get_outbox,
    send_mail,
)

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


async def test_memory_backend():
    print("\n--- memory backend ---")
    configure_mail(backend="memory")
    clear_outbox()
    ok = await send_mail(subject="Hi", body="Body", recipients=["a@example.com"])
    check("memory send returns True", ok is True)
    outbox = get_outbox()
    check("memory outbox has 1 message", len(outbox) == 1)
    # EMAIL_SUBJECT_PREFIX may be prepended by send().
    check("memory message subject", outbox[0].subject.endswith("Hi"))


async def test_console_backend():
    print("\n--- console backend ---")
    configure_mail(backend="console")
    ok = await send_mail(subject="Console", body="Body", recipients=["a@example.com"])
    check("console send returns True", ok is True)


async def test_header_injection_rejected():
    print("\n--- header injection rejected ---")
    configure_mail(backend="memory")
    clear_outbox()

    # CRLF in subject
    raised = False
    try:
        await EmailMessage(
            subject="Hi\r\nBcc: victim@example.com",
            body="x",
            recipients=["a@example.com"],
        ).send()
    except ValueError:
        raised = True
    check("CRLF in subject raises ValueError", raised)

    # newline in recipient
    raised = False
    try:
        await EmailMessage(
            subject="Hi",
            body="x",
            recipients=["a@example.com\nBcc: victim@example.com"],
        ).send()
    except ValueError:
        raised = True
    check("LF in recipient raises ValueError", raised)

    # CR in custom header value
    raised = False
    try:
        await EmailMessage(
            subject="Hi",
            body="x",
            recipients=["a@example.com"],
            headers={"X-Custom": "ok\r\nEvil: yes"},
        ).send()
    except ValueError:
        raised = True
    check("CRLF in custom header raises ValueError", raised)

    # Nothing leaked into the outbox
    check("no injected message reached outbox", len(get_outbox()) == 0)

    # A clean message still sends
    clear_outbox()
    ok = await EmailMessage(
        subject="Clean subject",
        body="x",
        recipients=["a@example.com"],
    ).send()
    check("clean message still sends", ok is True and len(get_outbox()) == 1)


async def test_timeout_honored():
    print("\n--- timeout honored ---")
    # configure_mail(timeout=) must reach smtplib. We intercept the SMTP
    # constructor to capture the timeout argument without a real server.
    captured = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None, **kw):
            captured["timeout"] = timeout
            captured["host"] = host
            captured["port"] = port

        def starttls(self, *a, **kw):
            pass

        def login(self, *a, **kw):
            pass

        def sendmail(self, *a, **kw):
            pass

        def quit(self):
            pass

    orig_smtp = mail_mod.smtplib.SMTP
    mail_mod.smtplib.SMTP = _FakeSMTP
    try:
        configure_mail(backend="smtp", host="mail.test", port=2525, timeout=7)
        check("config.timeout stored", get_mail_config().timeout == 7)
        ok = await send_mail(subject="T", body="b", recipients=["a@example.com"])
        check("smtp send returns True", ok is True)
        check(
            "timeout passed to SMTP client",
            captured.get("timeout") == 7,
            f"got {captured.get('timeout')!r}",
        )
        check("host passed to SMTP client", captured.get("host") == "mail.test")
    finally:
        mail_mod.smtplib.SMTP = orig_smtp
        configure_mail(backend="memory")


async def test_smtp_offloaded_to_thread():
    print("\n--- smtp runs off the event loop ---")
    # If _send_smtp ran inline on the loop, a blocking sleep in it would stall
    # a concurrent coroutine. Verify the loop keeps ticking during the send.
    import threading

    loop_thread = threading.get_ident()
    send_thread = {}

    class _BlockingSMTP:
        def __init__(self, host, port, timeout=None, **kw):
            send_thread["id"] = threading.get_ident()

        def starttls(self, *a, **kw):
            pass

        def login(self, *a, **kw):
            pass

        def sendmail(self, *a, **kw):
            pass

        def quit(self):
            pass

    orig_smtp = mail_mod.smtplib.SMTP
    mail_mod.smtplib.SMTP = _BlockingSMTP
    try:
        configure_mail(backend="smtp", host="mail.test", port=2525)
        await send_mail(subject="T", body="b", recipients=["a@example.com"])
        check(
            "SMTP executed on a worker thread, not the loop thread",
            send_thread.get("id") is not None and send_thread["id"] != loop_thread,
        )
    finally:
        mail_mod.smtplib.SMTP = orig_smtp
        configure_mail(backend="memory")


def main():
    print("=" * 60)
    print("Email Backend Tests")
    print("=" * 60)

    asyncio.run(test_memory_backend())
    asyncio.run(test_console_backend())
    asyncio.run(test_header_injection_rejected())
    asyncio.run(test_timeout_honored())
    asyncio.run(test_smtp_offloaded_to_thread())

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print("=" * 60)
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
