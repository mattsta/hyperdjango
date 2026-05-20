"""
API key authentication.

Validates API keys from headers or query parameters.
Keys are stored as SHA-256 hashes for memory safety — if the process
memory is dumped, raw API keys are not exposed.

When a TokenEngine is configured, incoming keys are verified via HMAC
first (no DB hit), rejecting forged keys instantly before hash comparison.
"""

import hashlib
import hmac


def _hash_api_key(key: str) -> str:
    """Hash an API key with SHA-256 for storage comparison."""
    return hashlib.sha256(key.encode()).hexdigest()


class APIKeyAuth:
    """API key authentication middleware.

    Checks for an API key in the specified header or query parameter.
    Keys are hashed on init for memory safety.

    When ``token_engine`` is provided, incoming keys are first verified
    via HMAC signature (instant rejection of forged keys without touching
    the hash set). The ``key_prefix`` is stripped before verification.

    Usage:
        # Simple static keys
        app.use(APIKeyAuth(
            valid_keys={"sk_live_abc123", "sk_live_def456"},
            header="X-API-Key",
        ))

        # With TokenEngine (signed keys, instant forgery rejection)
        from hyperdjango.signing import TokenEngine, SigningKey
        engine = TokenEngine(keys=[SigningKey(secret="key-secret", version=1)])
        app.use(APIKeyAuth(
            valid_keys={"sk_live_abc123"},
            header="X-API-Key",
            token_engine=engine,
            key_prefix="sk_live_",
        ))
    """

    def __init__(
        self,
        valid_keys=None,
        header="x-api-key",
        query_param=None,
        validate_func=None,
        token_engine=None,
        key_prefix="",
    ):
        # Store hashed keys — raw keys never kept in memory
        self._hashed_keys = {_hash_api_key(k) for k in (valid_keys or [])}
        self.header = header.lower()
        self.query_param = query_param
        self.validate_func = validate_func
        # TokenEngine for signed key verification (optional)
        self.token_engine = token_engine
        self.key_prefix = key_prefix

    def _verify_key(self, api_key: str) -> bool:
        """Verify an API key.

        When token_engine is set: HMAC verification first (instant forgery
        rejection), then hash comparison against stored keys.
        Without token_engine: direct hash comparison.
        """
        if self.token_engine is not None:
            # Strip display prefix
            if self.key_prefix and api_key.startswith(self.key_prefix):
                signed_part = api_key[len(self.key_prefix) :]
            else:
                signed_part = api_key

            # Phase 1: HMAC verification (rejects forgeries without DB hit)
            reference = self.token_engine.decode_ref(signed_part)
            if reference is None:
                return False

            # Phase 2: Hash lookup against stored keys
            ref_hash = _hash_api_key(reference)
            return any(
                hmac.compare_digest(ref_hash, stored) for stored in self._hashed_keys
            )

        # No token_engine: direct hash comparison
        incoming_hash = _hash_api_key(api_key)
        return any(
            hmac.compare_digest(incoming_hash, stored) for stored in self._hashed_keys
        )

    async def __call__(self, request, call_next):
        # Try header first
        api_key = request.headers.get(self.header)

        # Try query parameter
        if api_key is None and self.query_param:
            api_key = request.query(self.query_param)

        if api_key:
            if self.validate_func:
                request.api_key = api_key
                request.api_key_valid = await self.validate_func(api_key)
            else:
                request.api_key = api_key
                request.api_key_valid = self._verify_key(api_key)
        else:
            request.api_key = None
            request.api_key_valid = False

        return await call_next(request)


api_key_auth = APIKeyAuth()
