# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's [private vulnerability
reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository: **Security → Report a vulnerability**. That creates a
private advisory only the maintainers can see, and it becomes the place the fix
and disclosure are coordinated.

Useful things to include, roughly in order of value:

- what an attacker gains (read another tenant's secrets, forge a session,
  bypass a permission check) — impact first, mechanism second;
- the smallest reproduction you have, ideally a test against a bundled service;
- the commit or release you observed it on.

You do not need a polished write-up or a suggested patch. A rough report of a
real problem is worth far more than a delayed perfect one.

## Scope

This repository ships a web framework plus complete services built on it under
`services/`. Both are in scope, in particular the parts holding security
decisions:

- authentication and sessions (`hyperdjango/auth/`), signed tokens and key
  rotation (`signing.py`), HMAC helpers (`native/_crypto.py`);
- authorization — RBAC, guards, object- and field-level permissions;
- the request path: the native HTTP/WebSocket server (`zig/src/`), header and
  body parsing, multipart, CSRF, CORS, rate limiting, security headers;
- SQL construction in the ORM (injection reachable from user input);
- template rendering and autoescaping, including sandbox mode;
- `services/hypersecret` — the secret manager's envelope encryption, identity
  tokens, and namespace authorization boundary.

Out of scope: findings that require an attacker who already has shell access or
database credentials on the host; missing hardening in a _documented_ insecure
development default (`DEBUG=True`, an auto-generated per-process session secret)
where production explicitly requires the setting; and vulnerabilities in
PostgreSQL, Python, or Zig themselves — report those upstream.

## What to expect

This project is maintained by a very small team, so response times are
best-effort rather than contractual. What is promised: a private acknowledgement
that a human has read your report, an honest assessment of whether it is
exploitable and how severely, and credit in the advisory unless you would rather
not be named.

If a report turns out not to be a vulnerability, you will get the reasoning
rather than silence — a misunderstanding of the trust model is usually worth
fixing in the documentation.

## Supported versions

Development is trunk-only: fixes land on `main` and ship in the next release
stamp. There are no maintained release branches, so "upgrade to the current
`main`" is the remediation path for every confirmed issue.
