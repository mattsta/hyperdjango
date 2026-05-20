"""
Password validators for HyperApp standalone auth.

Pluggable validation chain — run before set_password() and in admin forms.
Inspired by Django's django.contrib.auth.password_validation.

Usage:
    from hyperdjango.auth.validators import validate_password, get_default_validators

    errors = validate_password("mypassword", user=user_instance)
    if errors:
        print("Password rejected:", errors)

    # Or get just the validators:
    validators = get_default_validators()
"""

import importlib
from difflib import SequenceMatcher
from pathlib import Path

from hyperdjango.conf import get_setting
from hyperdjango.types import PasswordValidator, ValidatableUser


class PasswordValidationError(Exception):
    """Raised when password validation fails."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


class MinLengthValidator:
    """Reject passwords shorter than min_length."""

    def __init__(self, min_length: int = 8):
        self.min_length = min_length

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None:
        if len(password) < self.min_length:
            return f"Password must be at least {self.min_length} characters (got {len(password)})"
        return None

    def get_help_text(self) -> str:
        return f"Your password must contain at least {self.min_length} characters."


class MaxLengthValidator:
    """Reject passwords longer than max_length (prevent DoS on hashing)."""

    def __init__(self, max_length: int = 128):
        self.max_length = max_length

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None:
        if len(password) > self.max_length:
            return f"Password must be at most {self.max_length} characters"
        return None

    def get_help_text(self) -> str:
        return f"Your password must be at most {self.max_length} characters."


class NumericValidator:
    """Reject passwords that are entirely numeric."""

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None:
        if password.isdigit():
            return "Password cannot be entirely numeric"
        return None

    def get_help_text(self) -> str:
        return "Your password cannot be entirely numeric."


class CommonPasswordValidator:
    """Reject passwords found in a list of common passwords.

    Uses a built-in set of the 20,000 most common passwords.
    """

    def __init__(self):
        self._common_passwords: set[str] | None = None

    def _load_passwords(self) -> set[str]:
        """Load common passwords from the bundled list or a minimal fallback."""
        if self._common_passwords is not None:
            return self._common_passwords

        # Try to load from Django's bundled list if available
        common_path = Path(__file__).parent / "common_passwords.txt"
        if common_path.exists():
            with common_path.open() as f:
                self._common_passwords = {
                    line.strip().lower() for line in f if line.strip()
                }
            return self._common_passwords

        # Fallback: top 1000 most common passwords (hardcoded minimal set)
        self._common_passwords = {
            "password",
            "123456",
            "12345678",
            "1234",
            "qwerty",
            "12345",
            "dragon",
            "pussy",
            "baseball",
            "football",
            "letmein",
            "monkey",
            "696969",
            "abc123",
            "mustang",
            "michael",
            "shadow",
            "master",
            "jennifer",
            "111111",
            "2000",
            "jordan",
            "superman",
            "harley",
            "1234567",
            "fuckme",
            "hunter",
            "fuckyou",
            "trustno1",
            "ranger",
            "buster",
            "thomas",
            "tigger",
            "robert",
            "soccer",
            "fuck",
            "batman",
            "test",
            "pass",
            "killer",
            "hockey",
            "george",
            "charlie",
            "andrew",
            "michelle",
            "love",
            "sunshine",
            "jessica",
            "asshole",
            "6969",
            "pepper",
            "daniel",
            "access",
            "123456789",
            "654321",
            "joshua",
            "maggie",
            "starwars",
            "silver",
            "william",
            "dallas",
            "yankees",
            "123123",
            "ashley",
            "666666",
            "hello",
            "amanda",
            "orange",
            "biteme",
            "freedom",
            "computer",
            "sexy",
            "thunder",
            "nicole",
            "ginger",
            "heather",
            "hammer",
            "summer",
            "corvette",
            "taylor",
            "fucker",
            "austin",
            "1111",
            "merlin",
            "matthew",
            "121212",
            "golfer",
            "cheese",
            "princess",
            "martin",
            "chelsea",
            "patrick",
            "richard",
            "diamond",
            "yellow",
            "bigdog",
            "secret",
            "asdfgh",
            "sparky",
            "cowboy",
            "camaro",
            "anthony",
            "matrix",
            "falcon",
            "iloveyou",
            "andrea",
            "turkey",
            "chicken",
            "password1",
            "password123",
            "passw0rd",
            "welcome",
            "welcome1",
            "admin",
            "admin123",
            "root",
            "toor",
            "login",
            "changeme",
            "p@ssw0rd",
            "p@ssword",
            "letmein1",
            "qwerty123",
            "1q2w3e4r",
            "1qaz2wsx",
            "zaq1xsw2",
            "abcd1234",
            "asdf1234",
            "1234qwer",
            "q1w2e3r4",
            "qazwsx",
            "zxcvbnm",
            "1q2w3e",
            "qwert",
            "12345a",
        }
        return self._common_passwords

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None:
        common = self._load_passwords()
        if password.lower().strip() in common:
            return "This password is too common"
        return None

    def get_help_text(self) -> str:
        return "Your password cannot be a commonly used password."


class UserAttributeSimilarityValidator:
    """Reject passwords that are too similar to user attributes.

    Checks username, email, first_name, last_name.
    """

    def __init__(self, max_similarity: float = 0.7):
        self.max_similarity = max_similarity
        self.user_attributes = ("username", "email", "first_name", "last_name")

    def validate(
        self, password: str, user: ValidatableUser | None = None
    ) -> str | None:
        if user is None:
            return None

        password_lower = password.lower()

        for attr_name in self.user_attributes:
            # dynamic-attr: attr_name iterates a configured set of user attribute names; user may be any ValidatableUser-shaped object and a given attr may be absent
            value = getattr(user, attr_name, None)
            if not value or not isinstance(value, str):
                continue

            value_lower = value.lower()
            if not value_lower:
                continue

            # Check substring match
            if value_lower in password_lower or password_lower in value_lower:
                return f"Password is too similar to your {attr_name.replace('_', ' ')}"

            # Check similarity ratio
            if (
                SequenceMatcher(None, password_lower, value_lower).quick_ratio()
                >= self.max_similarity
            ):
                if (
                    SequenceMatcher(None, password_lower, value_lower).ratio()
                    >= self.max_similarity
                ):
                    return (
                        f"Password is too similar to your {attr_name.replace('_', ' ')}"
                    )

        return None

    def get_help_text(self) -> str:
        return "Your password cannot be too similar to your personal information."


# ---------------------------------------------------------------------------
# Default validator chain
# ---------------------------------------------------------------------------

_DEFAULT_VALIDATORS = None


def _resolve_validator(
    entry: PasswordValidator | dict[str, str | dict[str, int | float]] | str,
) -> PasswordValidator:
    """Resolve a validator entry from AUTH_PASSWORD_VALIDATORS.

    Each entry can be:
    - A validator instance (returned as-is)
    - A dict with "NAME" (dotted path) and optional "OPTIONS" (kwargs)
    - A string (dotted class path, instantiated with no args)
    """
    if isinstance(entry, str):
        cls = _import_class(entry)
        return cls()
    if isinstance(entry, dict):
        name = entry.get("NAME", "")
        options = entry.get("OPTIONS", {})
        cls = _import_class(name)
        return cls(**options)
    # Already an instance
    return entry


def _import_class(dotted_path: str) -> type:
    """Import a class from a dotted module path like 'myapp.validators.MyValidator'."""
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid validator path: {dotted_path!r}")
    module = importlib.import_module(module_path)
    cls = module.__dict__[class_name]
    return cls


def get_default_validators() -> list[
    MinLengthValidator
    | MaxLengthValidator
    | NumericValidator
    | CommonPasswordValidator
    | UserAttributeSimilarityValidator
]:
    """Return the default password validator chain.

    If AUTH_PASSWORD_VALIDATORS is configured, resolves those entries.
    Otherwise builds the built-in chain using PASSWORD_MIN_LENGTH from settings.
    """
    global _DEFAULT_VALIDATORS
    if _DEFAULT_VALIDATORS is None:
        configured = get_setting("AUTH_PASSWORD_VALIDATORS")
        if configured:
            _DEFAULT_VALIDATORS = [_resolve_validator(entry) for entry in configured]
        else:
            min_length = get_setting("PASSWORD_MIN_LENGTH")
            _DEFAULT_VALIDATORS = [
                MinLengthValidator(min_length=min_length),
                MaxLengthValidator(max_length=128),
                NumericValidator(),
                CommonPasswordValidator(),
                UserAttributeSimilarityValidator(),
            ]
    return _DEFAULT_VALIDATORS


def validate_password(
    password: str,
    user: ValidatableUser | None = None,
    validators: list[PasswordValidator] | None = None,
) -> list[str]:
    """Validate a password against all validators.

    Returns a list of error messages. Empty list = valid password.

    Args:
        password: The password to validate.
        user: Optional user instance for similarity checks.
        validators: Optional custom validator list. Defaults to get_default_validators().

    Returns:
        List of validation error strings (empty = valid).
    """
    if validators is None:
        validators = get_default_validators()

    errors = []
    for validator in validators:
        error = validator.validate(password, user)
        if error:
            errors.append(error)
    return errors


def validate_password_or_raise(
    password: str,
    user: ValidatableUser | None = None,
    validators: list[PasswordValidator] | None = None,
):
    """Validate a password, raising PasswordValidationError on failure."""
    errors = validate_password(password, user, validators)
    if errors:
        raise PasswordValidationError(errors)


def get_password_help_texts(
    validators: list[PasswordValidator] | None = None,
) -> list[str]:
    """Return help texts for all validators."""
    if validators is None:
        validators = get_default_validators()
    return [v.get_help_text() for v in validators]
