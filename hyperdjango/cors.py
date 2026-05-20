"""CORS policy — the single authority for the cross-origin access decision.

Both the ASGI ``CORSMiddleware`` (``hyperdjango.standalone_middleware``) and the
Django ``HyperCORSMiddleware`` (``hyperdjango.serving.django_middleware``) resolve
a request's ``Origin`` through this one class, so they can never drift into
subtly different — and differently (in)secure — cross-origin behaviour.
"""

from dataclasses import InitVar, dataclass, field
from typing import NamedTuple


class CorsDecision(NamedTuple):
    """What CORS headers a response should carry for one request Origin."""

    allow_origin: str  # value for Access-Control-Allow-Origin
    allow_credentials: bool  # whether to send Access-Control-Allow-Credentials: true
    vary_origin: bool  # whether the response must Vary: Origin (it echoes the Origin)


@dataclass(slots=True)
class CorsPolicy:
    """Decides Access-Control-Allow-Origin / -Credentials / Vary for an Origin.

    Constructed once from the configured origin allowlist and credentials flag;
    ``resolve(origin)`` then returns the per-request decision (or ``None`` when
    the Origin is not allowed and no CORS headers should be emitted).
    """

    origins: InitVar[object]
    allow_credentials: bool = False
    allow_any_origin: bool = field(init=False)
    _origins: frozenset = field(init=False)

    def __post_init__(self, origins) -> None:
        self.allow_any_origin = "*" in origins
        self._origins = frozenset(origins)
        # Reflecting an arbitrary request Origin while also sending
        # Access-Control-Allow-Credentials: true lets ANY site read a
        # credentialed (cookie/authorized) response — account takeover. The
        # wildcard is incompatible with credentials per the Fetch spec, so
        # refuse the combination at construction rather than silently reflecting.
        if self.allow_any_origin and self.allow_credentials:
            raise ValueError(
                "CORS: origins=['*'] cannot be combined with allow_credentials=True. "
                "Specify an explicit origin allowlist so credentialed responses are "
                "only exposed to trusted origins."
            )

    def resolve(self, origin: str) -> CorsDecision | None:
        """Decide the CORS headers for a request ``Origin`` (empty string if none).

        Returns a :class:`CorsDecision`, or ``None`` when the Origin is not
        allowed (the caller emits no CORS headers).
        """
        if self.allow_any_origin:
            # The literal "*" is origin-independent and (per the guard above)
            # never paired with credentials, so it needs neither credentials
            # nor Vary: Origin.
            return CorsDecision("*", False, False)
        if origin and origin in self._origins:
            # Echoing this specific origin back makes the response vary by Origin.
            return CorsDecision(origin, self.allow_credentials, True)
        return None
