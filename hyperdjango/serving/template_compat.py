"""
Django template tag compatibility for the Zig template engine.

Registers Django's most-used template functions as template filters/globals
so they work in Zig-compiled templates without {% load %}.

Common Django tags mapped to Zig-compatible equivalents:
- {% static 'path' %}     -> {{ 'path'|static }}
- {% url 'name' arg %}    -> {{ url('name', arg) }}  (via context function)
- {% csrf_token %}         -> {{ csrf_token }}  (via context variable)
- {% trans 'text' %}       -> {{ 'text'|trans }}  (i18n filter)

Usage:
    register_django_compat(engine)  # called by ZigTemplates.__init__
"""

import html as _html

try:
    # Django's mark_safe returns a SafeString whose __html__ the native template
    # engine honors (writeValue), so intentionally-HTML filter output is not
    # re-escaped. Fall back to a minimal SafeString when Django is absent.
    from django.utils.safestring import mark_safe as _mark_safe
except ImportError:

    class _SafeStr(str):
        """Minimal already-safe HTML string (implements the __html__ protocol)."""

        def __html__(self):
            return str(self)

    def _mark_safe(value):
        return _SafeStr(value)


try:
    from django.templatetags.static import static as _django_static
except ImportError:
    _django_static = None

try:
    from django.urls import NoReverseMatch as _NoReverseMatch
    from django.urls import reverse as _django_reverse
except ImportError:
    _django_reverse = None
    _NoReverseMatch = None

try:
    from django.utils.translation import gettext as _django_gettext
except ImportError:
    _django_gettext = None

try:
    from django.utils.dateformat import format as _django_date_format
    from django.utils.dateformat import time_format as _django_time_format
except ImportError:
    _django_date_format = None
    _django_time_format = None


def static_filter(path):
    """Resolve a static file path using Django's static file finder."""
    if _django_static is not None:
        return _django_static(path)
    return f"/static/{path}"


def url_filter(name, *args):
    """Reverse a Django URL by name."""
    if _django_reverse is not None:
        try:
            return _django_reverse(name, args=args or None)
        except _NoReverseMatch:
            return f"/{name}/"
    return f"/{name}/"


def trans_filter(text):
    """Translate text using Django's i18n system."""
    if _django_gettext is not None:
        return _django_gettext(text)
    return text


def date_filter(value, format_str="N j, Y"):
    """Format a date using Django's date format strings."""
    if _django_date_format is not None:
        try:
            return _django_date_format(value, format_str)
        # blind-except: a template date filter degrades to the value's string form for any unformattable value rather than aborting page render.
        except Exception:
            return str(value)
    return str(value)


def time_filter(value, format_str="P"):
    """Format a time using Django's time format strings."""
    if _django_time_format is not None:
        try:
            return _django_time_format(value, format_str)
        # blind-except: a template time filter degrades to the value's string form for any unformattable value rather than aborting page render.
        except Exception:
            return str(value)
    return str(value)


def pluralize_filter(value, suffix="s"):
    """Return suffix if value is not 1."""
    try:
        count = int(value)
        if count == 1:
            return ""
        return suffix
    except ValueError, TypeError:
        return suffix


def truncatewords_filter(value, count="30"):
    """Truncate text to N words."""
    try:
        n = int(count)
        words = str(value).split()
        if len(words) <= n:
            return str(value)
        return " ".join(words[:n]) + "..."
    except ValueError, TypeError:
        return str(value)


def linebreaks_filter(value):
    """Convert newlines to <br> tags. Escapes HTML first to prevent XSS.

    Returns a mark_safe / SafeString value so the native engine honors the
    intentional <br> markup via the __html__ protocol instead of re-escaping it
    into a literal ``&lt;br&gt;``. The user-supplied text is already escaped
    above, so this is safe.
    """
    escaped = _html.escape(str(value))
    return _mark_safe(escaped.replace("\n", "<br>"))


def yesno_filter(value, choices="yes,no,maybe"):
    """Map True/False/None to custom strings."""
    parts = choices.split(",")
    if value is True:
        return parts[0] if parts else "yes"
    elif value is False:
        return parts[1] if len(parts) > 1 else "no"
    else:
        return parts[2] if len(parts) > 2 else "maybe"


def register_django_compat(engine):
    """Register Django-compatible filters and globals on a TemplateEngine.

    Called by ZigTemplates.__init__ to make common Django template
    patterns work in Zig-compiled templates.
    """
    # Filters
    engine.add_filter("static", static_filter)
    engine.add_filter("trans", trans_filter)
    engine.add_filter("date", date_filter)
    engine.add_filter("time", time_filter)
    engine.add_filter("pluralize", pluralize_filter)
    engine.add_filter("truncatewords", truncatewords_filter)
    engine.add_filter("linebreaks", linebreaks_filter)
    engine.add_filter("yesno", yesno_filter)

    # Globals (available as context variables in all templates)
    engine.add_global("url", url_filter)
    engine.add_global("static", static_filter)
