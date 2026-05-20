"""
IETF-compliant rate limit client (draft-ietf-httpapi-ratelimit-headers-10).

Parses ``RateLimit-Policy`` and ``RateLimit`` response headers to track server
quota state and implement proper backoff. Respects ``Retry-After`` on 429/503.

Designed for:
- Inter-service HTTP calls
- Webhook delivery with backoff
- E2E test clients
- Any HTTP consumer that wants to respect server rate limits

Usage:
    from hyperdjango.ratelimit_client import RateLimitState

    state = RateLimitState()

    # Before each request:
    wait = state.wait_time()
    if wait > 0:
        await asyncio.sleep(wait)

    response = await http_client.get(url)

    # After each response:
    state.update_from_headers(response.status, response.headers)

    # Or use the high-level async helper:
    from hyperdjango.ratelimit_client import RateLimitedSession

    session = RateLimitedSession(base_url="http://api.example.com")
    resp = await session.get("/items")  # auto-waits, auto-updates state
"""

import contextlib
import random
import time
from dataclasses import dataclass, field

# ─── Structured Fields Parsers ────────────────────────────────────────────────
#
# Minimal RFC 9651 Structured Fields parsers for RateLimit headers only.
# Not a full SF parser — only handles the shapes we emit and consume.


def _parse_sf_string(raw: str, pos: int) -> tuple[str, int]:
    """Parse an sf-string starting at pos. Returns (value, new_pos)."""
    if pos >= len(raw) or raw[pos] != '"':
        return "", pos
    pos += 1  # skip opening quote
    chars: list[str] = []
    while pos < len(raw):
        ch = raw[pos]
        if ch == "\\":
            pos += 1
            if pos < len(raw):
                chars.append(raw[pos])
                pos += 1
        elif ch == '"':
            pos += 1
            break
        else:
            chars.append(ch)
            pos += 1
    return "".join(chars), pos


def _parse_sf_params(raw: str, pos: int) -> tuple[dict[str, int | str | bytes], int]:
    """Parse sf-parameters (;key=value pairs). Returns (params_dict, new_pos)."""
    params: dict[str, int | str | bytes] = {}
    while pos < len(raw) and raw[pos] == ";":
        pos += 1  # skip ;
        # Parse param name
        name_start = pos
        while pos < len(raw) and raw[pos] not in (";", "=", ",", " "):
            pos += 1
        name = raw[name_start:pos]
        if pos < len(raw) and raw[pos] == "=":
            pos += 1  # skip =
            if pos < len(raw) and raw[pos] == '"':
                # sf-string value
                val, pos = _parse_sf_string(raw, pos)
                params[name] = val
            elif pos < len(raw) and raw[pos] == ":":
                # sf-byte-sequence value
                end = raw.index(":", pos + 1)
                import base64

                params[name] = base64.b64decode(raw[pos + 1 : end])
                pos = end + 1
            else:
                # sf-integer value
                num_start = pos
                if pos < len(raw) and raw[pos] == "-":
                    pos += 1
                while pos < len(raw) and raw[pos].isdigit():
                    pos += 1
                params[name] = int(raw[num_start:pos])
        else:
            # Boolean parameter (bare name = true)
            params[name] = 1
    return params, pos


def parse_ratelimit_policy(header: str) -> list[dict[str, int | str | bytes]]:
    """Parse a RateLimit-Policy header value into a list of policy dicts.

    Each dict has:
        name: str           — policy identifier
        q: int              — quota
        w: int              — window seconds (0 if absent)
        qu: str             — quota unit ("requests" if absent)
        pk: bytes           — partition key (b"" if absent)

    Example:
        >>> parse_ratelimit_policy('"burst";q=100;w=60, "daily";q=1000;w=86400')
        [{"name": "burst", "q": 100, "w": 60, "qu": "requests", "pk": b""},
         {"name": "daily", "q": 1000, "w": 86400, "qu": "requests", "pk": b""}]
    """
    if not header or not header.strip():
        return []

    results: list[dict[str, int | str | bytes]] = []
    pos = 0
    raw = header.strip()

    while pos < len(raw):
        # Skip whitespace and commas
        while pos < len(raw) and raw[pos] in (" ", ","):
            pos += 1
        if pos >= len(raw):
            break

        # Parse the item key (sf-string)
        name, pos = _parse_sf_string(raw, pos)
        if not name:
            break

        # Parse parameters
        params, pos = _parse_sf_params(raw, pos)

        results.append(
            {
                "name": name,
                "q": params.get("q", 0),
                "w": params.get("w", 0),
                "qu": params.get("qu", "requests"),
                "pk": params.get("pk", b""),
            }
        )

    return results


def parse_ratelimit(header: str) -> list[dict[str, int | str | bytes]]:
    """Parse a RateLimit header value into a list of service limit dicts.

    Each dict has:
        policy_name: str    — matches a QuotaPolicy name
        r: int              — remaining quota units
        t: int              — seconds until reset (0 if absent)
        pk: bytes           — partition key (b"" if absent)

    Example:
        >>> parse_ratelimit('"default";r=50;t=30')
        [{"policy_name": "default", "r": 50, "t": 30, "pk": b""}]
    """
    if not header or not header.strip():
        return []

    results: list[dict[str, int | str | bytes]] = []
    pos = 0
    raw = header.strip()

    while pos < len(raw):
        while pos < len(raw) and raw[pos] in (" ", ","):
            pos += 1
        if pos >= len(raw):
            break

        name, pos = _parse_sf_string(raw, pos)
        if not name:
            break

        params, pos = _parse_sf_params(raw, pos)

        results.append(
            {
                "policy_name": name,
                "r": params.get("r", 0),
                "t": params.get("t", 0),
                "pk": params.get("pk", b""),
            }
        )

    return results


def parse_retry_after(header: str) -> int:
    """Parse a Retry-After header value.

    Supports:
    - Integer seconds: "30"
    - HTTP-date: "Mon, 05 Aug 2019 09:27:05 GMT" (converted to seconds from now)

    Returns seconds to wait. 0 if unparseable.
    """
    if not header:
        return 0
    header = header.strip()
    # Try integer first
    try:
        return max(0, int(header))
    except ValueError:
        pass
    # Try HTTP-date
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(header)
        delta = dt.timestamp() - time.time()
        return max(0, int(delta))
    except ValueError, TypeError:
        # Unparseable HTTP-date (parsedate_to_datetime raises ValueError/TypeError
        # on malformed input) — treat as "no wait".
        return 0


# ─── Policy Tracker ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class PolicyState:
    """Tracked state for a single quota policy."""

    name: str
    quota: int = 0
    window: int = 0
    quota_unit: str = "requests"
    remaining: int = 0
    reset_at: float = 0.0  # monotonic time when quota resets
    partition_key: bytes = b""


@dataclass
class RateLimitState:
    """Client-side rate limit state tracker.

    Parses IETF RateLimit headers from HTTP responses and tracks per-policy
    quota state. Computes wait times with jittered backoff.

    Attributes:
        policies: tracked policy states keyed by policy name
        blocked_until: monotonic time until which all requests should wait
        max_wait: maximum wait time in seconds (prevents DoS via huge reset values)
        jitter_factor: random jitter as fraction of wait time (0.0 = none, 0.2 = 20%)
    """

    max_wait: float = 300.0
    jitter_factor: float = 0.1
    policies: dict[str, PolicyState] = field(default_factory=dict)
    blocked_until: float = 0.0
    _last_status: int = field(default=0, init=False)

    def update_from_headers(
        self,
        status: int,
        headers: dict[str, str],
    ) -> None:
        """Update state from HTTP response headers.

        Call this after every HTTP response. Handles:
        - RateLimit-Policy → update quota/window definitions
        - RateLimit → update remaining/reset per policy
        - Retry-After → set global block (on 429/503)
        - Problem Details body parsing (optional)

        Args:
            status: HTTP response status code
            headers: Response headers (case-insensitive keys)
        """
        self._last_status = status
        now = time.monotonic()

        # Normalize header keys to lowercase
        h: dict[str, str] = {k.lower(): v for k, v in headers.items()}

        # Parse RateLimit-Policy (static policy definition)
        policy_hdr = h.get("ratelimit-policy", "")
        if policy_hdr:
            for p in parse_ratelimit_policy(policy_hdr):
                name = str(p["name"])
                ps = self.policies.get(name)
                if ps is None:
                    ps = PolicyState(name=name)
                    self.policies[name] = ps
                ps.quota = int(p["q"])
                ps.window = int(p["w"])
                ps.quota_unit = str(p["qu"])
                pk = p["pk"]
                if isinstance(pk, bytes):
                    ps.partition_key = pk

        # Parse RateLimit (dynamic per-request status)
        rl_hdr = h.get("ratelimit", "")
        if rl_hdr:
            for lim in parse_ratelimit(rl_hdr):
                name = str(lim["policy_name"])
                ps = self.policies.get(name)
                if ps is None:
                    ps = PolicyState(name=name)
                    self.policies[name] = ps
                ps.remaining = int(lim["r"])
                t = int(lim["t"])
                if t > 0:
                    ps.reset_at = now + min(t, self.max_wait)

        # Handle Retry-After on 429/503
        if status in (429, 503):
            retry_after = parse_retry_after(h.get("retry-after", ""))
            if retry_after > 0:
                self.blocked_until = now + min(retry_after, self.max_wait)

        # Fall back to legacy x-ratelimit-* headers if no IETF headers present
        if not rl_hdr and not policy_hdr:
            self._update_from_legacy(h, now, status)

    def _update_from_legacy(self, h: dict[str, str], now: float, status: int) -> None:
        """Fall back to parsing x-ratelimit-* headers."""
        limit_str = h.get("x-ratelimit-limit", "")
        remaining_str = h.get("x-ratelimit-remaining", "")
        reset_str = h.get("x-ratelimit-reset", "")

        if not limit_str and not remaining_str:
            return

        name = "default"
        ps = self.policies.get(name)
        if ps is None:
            ps = PolicyState(name=name)
            self.policies[name] = ps

        if limit_str:
            with contextlib.suppress(ValueError):
                ps.quota = int(limit_str)
        if remaining_str:
            with contextlib.suppress(ValueError):
                ps.remaining = int(remaining_str)
        if reset_str:
            with contextlib.suppress(ValueError):
                ps.reset_at = now + min(int(reset_str), self.max_wait)

    def wait_time(self) -> float:
        """Compute how many seconds the client should wait before the next request.

        Returns 0.0 if no waiting is needed.

        Logic (per RFC recommendations):
        1. If blocked by Retry-After → wait until blocked_until
        2. If any policy has remaining=0 → wait until its reset_at
        3. Otherwise → 0.0 (proceed immediately)
        4. Jitter is added to prevent thundering herd

        The returned value is capped at max_wait.
        """
        now = time.monotonic()

        # Check global block (Retry-After)
        if self.blocked_until > now:
            wait = self.blocked_until - now
            # Cap AFTER jitter: jitter must never push the wait past max_wait
            # (jittering min(wait, max_wait) could exceed max_wait). Result ∈ [0, max_wait].
            return min(self._add_jitter(wait), self.max_wait)

        # Check per-policy exhaustion
        max_policy_wait = 0.0
        for ps in self.policies.values():
            if ps.remaining <= 0 and ps.reset_at > now:
                policy_wait = ps.reset_at - now
                max_policy_wait = max(max_policy_wait, policy_wait)

        if max_policy_wait > 0:
            # Cap AFTER jitter so the returned wait is always within [0, max_wait].
            return min(self._add_jitter(max_policy_wait), self.max_wait)

        return 0.0

    def should_proceed(self) -> bool:
        """Return True if the client should send the next request now.

        Equivalent to ``wait_time() == 0.0`` but avoids the jitter computation.
        """
        now = time.monotonic()
        if self.blocked_until > now:
            return False
        for ps in self.policies.values():
            if ps.remaining <= 0 and ps.reset_at > now:
                return False
        return True

    def most_restrictive_policy(self) -> PolicyState | None:
        """Return the policy with the lowest remaining quota.

        Useful for adaptive request pacing — spread remaining quota evenly
        over the reset window.
        """
        if not self.policies:
            return None
        return min(self.policies.values(), key=lambda ps: ps.remaining)

    def pace_interval(self) -> float:
        """Compute the optimal interval between requests to avoid hitting the limit.

        Returns seconds between requests, or 0.0 if no pacing is needed.

        Based on: remaining / time_until_reset = safe requests per second.
        Inverse gives the interval.
        """
        now = time.monotonic()
        min_interval = 0.0

        for ps in self.policies.values():
            if ps.remaining <= 0:
                continue
            time_left = ps.reset_at - now
            if time_left <= 0:
                continue
            # Safe rate: remaining / time_left requests per second
            # Interval: time_left / remaining
            interval = time_left / ps.remaining
            min_interval = max(min_interval, interval)

        return min_interval

    def reset(self) -> None:
        """Clear all tracked state."""
        self.policies.clear()
        self.blocked_until = 0.0
        self._last_status = 0

    def _add_jitter(self, wait: float) -> float:
        """Add random jitter to prevent thundering herd."""
        if self.jitter_factor <= 0 or wait <= 0:
            return wait
        jitter = wait * self.jitter_factor * random.random()
        return wait + jitter

    @property
    def is_rate_limited(self) -> bool:
        """True if the last response was a 429."""
        return self._last_status == 429

    @property
    def is_service_unavailable(self) -> bool:
        """True if the last response was a 503 (temporary reduced capacity)."""
        return self._last_status == 503


# ─── Problem Details Parser ──────────────────────────────────────────────────


@dataclass(slots=True)
class RateLimitProblem:
    """Parsed RFC 9457 Problem Details from a rate limit 429/503 response."""

    problem_type: str = ""
    title: str = ""
    status: int = 0
    detail: str = ""
    violated_policies: list[str] = field(default_factory=list)
    retry_after: int = 0


def parse_problem_detail(body: str | bytes | dict[str, object]) -> RateLimitProblem:
    """Parse an RFC 9457 Problem Details JSON body from a rate limit response.

    Accepts raw string/bytes (JSON-decoded) or an already-parsed dict.
    Returns a RateLimitProblem with extracted fields.
    """
    if isinstance(body, (str, bytes)):
        import json

        try:
            data = json.loads(body)
        except json.JSONDecodeError, ValueError:
            return RateLimitProblem()
    else:
        data = body

    if not isinstance(data, dict):
        return RateLimitProblem()

    violated = data.get("violated-policies", [])
    if not isinstance(violated, list):
        violated = []

    return RateLimitProblem(
        problem_type=str(data.get("type", "")),
        title=str(data.get("title", "")),
        status=int(data.get("status", 0)),
        detail=str(data.get("detail", "")),
        violated_policies=[str(v) for v in violated],
        retry_after=int(data.get("retry_after", 0)),
    )
