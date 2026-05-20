"""
Django authentication backends for HyperDjango.

Provides OAuth2 authentication that integrates with Django's auth system:
- django.contrib.auth.login() / logout()
- @login_required decorator
- Django admin login
- request.user populated by Django's AuthenticationMiddleware

Usage in Django settings:

    AUTHENTICATION_BACKENDS = [
        'hyperdjango.serving.auth_backends.OAuth2Backend',
        'django.contrib.auth.backends.ModelBackend',  # password fallback
    ]

    HYPERDJANGO_OAUTH2_PROVIDERS = {
        'google': {
            'client_id': '...',
            'client_secret': '...',
        },
        'github': {
            'client_id': '...',
            'client_secret': '...',
        },
    }
    HYPERDJANGO_OAUTH2_CREATE_USER = True  # auto-create users on first login
    HYPERDJANGO_OAUTH2_UPDATE_USER = True  # update user fields on each login
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend

from hyperdjango.logging import logger


class OAuth2Backend(BaseBackend):
    """Django authentication backend for OAuth2 providers.

    Authenticates users via OAuth2 profile data from the callback.
    Creates or updates Django User objects automatically.

    Called by hyperdjango's OAuth2 callback view via:
        django.contrib.auth.authenticate(
            request,
            oauth2_provider='google',
            oauth2_profile={'email': '...', 'name': '...', ...}
        )
    """

    def authenticate(
        self, request, oauth2_provider=None, oauth2_profile=None, **kwargs
    ):
        """Authenticate a user from an OAuth2 profile.

        Args:
            request: Django HttpRequest
            oauth2_provider: Provider name ('google', 'github', 'auth0')
            oauth2_profile: Normalized user profile dict from provider
                           Keys: id, email, email_verified, name, avatar,
                           oauth2_provider, raw_profile

        Returns:
            Django User instance or None
        """
        if oauth2_provider is None or oauth2_profile is None:
            return None

        # Fail closed: this backend links (and auto-creates) accounts BY EMAIL, so
        # it must never trust an address the provider did not verify — that is a
        # direct account-takeover vector (an attacker sets any email on their IdP
        # account). extract_user_data() already zeroes unverified emails, but the
        # linking boundary must not depend on its caller for that guarantee. A
        # missing email_verified is treated as unverified (deny by default).
        if not oauth2_profile.get("email_verified"):
            logger.warning(
                "OAuth2 login with unverified email rejected from {provider}",
                provider=oauth2_provider,
            )
            return None

        email = oauth2_profile.get("email", "").strip().lower()
        if not email:
            logger.warning(
                "OAuth2 login without email from {provider}", provider=oauth2_provider
            )
            return None

        User = get_user_model()
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        create_user = getattr(settings, "HYPERDJANGO_OAUTH2_CREATE_USER", True)
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        update_user = getattr(settings, "HYPERDJANGO_OAUTH2_UPDATE_USER", True)

        # Look up existing user by email
        try:
            user = User.objects.get(email=email)
            if update_user:
                self._update_user_fields(user, oauth2_profile, oauth2_provider)
            return user
        except User.DoesNotExist:
            pass

        # Auto-create user if configured
        if not create_user:
            logger.info(
                "OAuth2 user {email} not found and auto-create disabled", email=email
            )
            return None

        user = self._create_user(User, oauth2_profile, oauth2_provider)
        return user

    def get_user(self, user_id):
        """Retrieve a user by primary key."""
        User = get_user_model()
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

    def _create_user(self, User, profile, provider):
        """Create a new Django User from OAuth2 profile."""
        email = profile.get("email", "").strip().lower()
        name = profile.get("name", "")

        # Split name into first/last
        parts = name.split(None, 1) if name else [""]
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        # Generate a username from email (before @)
        username = email.split("@")[0] if email else profile.get("id", "user")

        # Ensure unique username
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1

        user = User.objects.create_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )
        # Set unusable password — OAuth2 users don't need one
        user.set_unusable_password()
        user.save()

        logger.info(
            "Created OAuth2 user: {email} via {provider}",
            email=email,
            provider=provider,
        )
        return user

    def _update_user_fields(self, user, profile, provider):
        """Update user fields from OAuth2 profile."""
        changed = False
        name = profile.get("name", "")
        if name:
            parts = name.split(None, 1)
            if parts[0] and user.first_name != parts[0]:
                user.first_name = parts[0]
                changed = True
            if len(parts) > 1 and user.last_name != parts[1]:
                user.last_name = parts[1]
                changed = True

        if changed:
            user.save(update_fields=["first_name", "last_name"])
