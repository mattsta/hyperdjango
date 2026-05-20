"""
Django template backend for HyperDjango's native Zig template engine.

Drop-in replacement for django.template.backends.jinja2.Jinja2 that uses
native Zig compilation for 1.5x faster rendering.

Usage in Django settings:

    TEMPLATES = [{
        'BACKEND': 'hyperdjango.serving.template_backend.ZigTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'autoescape': True,
            'auto_reload': True,  # default: settings.DEBUG
            'cache_max_bytes': 256 * 1024 * 1024,  # 256 MB
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    }]
"""

from pathlib import Path

from django.conf import settings
from django.template import TemplateDoesNotExist, TemplateSyntaxError
from django.template.backends.base import BaseEngine
from django.utils.functional import cached_property
from django.utils.module_loading import import_string


class ZigTemplates(BaseEngine):
    """Django template backend using HyperDjango's native Zig template engine.

    Jinja2-compatible syntax with native Zig compilation. Supports:
    - Math expressions: {{ x + 1 }}, {{ a * b }}
    - String concat: {{ "hello" ~ name }}
    - Ternary: {{ x if cond else y }}
    - List/dict/tuple literals: {{ [1, 2, 3] }}
    - 42 native filters, 20 is-tests
    - For-loop tuple unpacking
    - {% with %} scoped blocks
    - Macros with parameters and defaults
    - Proper expression tokenizer + recursive descent parser
    """

    app_dirname = "templates"

    def __init__(self, params):
        params = params.copy()
        options = params.pop("OPTIONS", {}).copy()
        super().__init__(params)

        self.context_processors = options.pop("context_processors", [])

        # Import our template engine
        from hyperdjango.templating import TemplateEngine

        # dynamic-attr: DEBUG may be absent on a minimally-configured Django settings object
        auto_reload = options.pop("auto_reload", getattr(settings, "DEBUG", True))
        autoescape = options.pop("autoescape", True)
        cache_max_bytes = options.pop("cache_max_bytes", 256 * 1024 * 1024)

        # Create engine — it handles multiple template dirs via search
        self.engine = TemplateEngine(
            template_dir=str(self.dirs[0]) if self.dirs else "templates",
            auto_reload=auto_reload,
            autoescape=autoescape,
            cache_max_bytes=cache_max_bytes,
        )

        # Store all template dirs for multi-directory search
        self._template_dirs = list(self.template_dirs)

        # Register Django template tag compatibility (static, url, trans, etc.)
        from hyperdjango.serving.template_compat import register_django_compat

        register_django_compat(self.engine)

    def from_string(self, template_code):
        """Create a template from inline source code."""
        return ZigTemplate(self.engine, None, source=template_code, backend=self)

    def get_template(self, template_name):
        """Load a template by name from configured directories."""
        # Search all template dirs
        for template_dir in self._template_dirs:
            filepath = Path(template_dir) / template_name
            if filepath.is_file():
                return ZigTemplate(
                    self.engine, template_name, filepath=str(filepath), backend=self
                )

        raise TemplateDoesNotExist(template_name, backend=self)

    @cached_property
    def template_context_processors(self):
        return [import_string(path) for path in self.context_processors]


class ZigTemplate:
    """Django template interface wrapping a Zig-compiled template."""

    def __init__(self, engine, template_name, filepath=None, source=None, backend=None):
        self.engine = engine
        self.template_name = template_name
        self.filepath = filepath
        self.source = source
        self.backend = backend
        self.origin = ZigOrigin(
            name=filepath or "<string>",
            template_name=template_name or "<string>",
        )

    def render(self, context=None, request=None):
        """Render the template with context and optional request.

        Applies Django context processors when a request is provided.
        """
        if context is None:
            context = {}
        else:
            # Flatten Django Context objects to plain dict
            if hasattr(context, "flatten"):
                context = context.flatten()
            else:
                context = dict(context)

        if request is not None:
            context["request"] = request
            # Apply CSRF token helpers
            try:
                from django.template.backends.utils import (
                    csrf_input_lazy,
                    csrf_token_lazy,
                )

                context["csrf_input"] = csrf_input_lazy(request)
                context["csrf_token"] = csrf_token_lazy(request)
            except ImportError:
                pass
            # Apply context processors
            if self.backend:
                for processor in self.backend.template_context_processors:
                    context.update(processor(request))

        try:
            if self.source is not None:
                return self.engine.render_string(self.source, context)
            return self.engine.render(self.template_name, context)
        except FileNotFoundError as exc:
            raise TemplateDoesNotExist(self.template_name) from exc
        except Exception as exc:
            raise TemplateSyntaxError(str(exc)) from exc


class ZigOrigin:
    """Template origin for debug information."""

    def __init__(self, name, template_name):
        self.name = name
        self.template_name = template_name
