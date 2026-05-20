# Site Configuration (White-Label Deployment)

HyperDjango's site configuration system lets you brand and customize apps without modifying templates. All branding, theming, and app-level settings are defined in frozen dataclasses, loaded from TOML files or environment variables, and injected into templates automatically.

---

## Quick Start

```python
from hyperdjango import HyperApp
from hyperdjango.site_config import SiteConfig, ThemeColors

config = SiteConfig(
    name="My Product",
    tagline="Fast and reliable",
    footer_text="Powered by My Company",
    theme=ThemeColors(primary="#3b82f6", background="#f0f4f8"),
)

app = HyperApp(site_config=config)
```

Templates automatically get `{{ site.name }}`, `{{ site.tagline }}`, `{{ site_css }}`, etc.

---

## Core Classes

### ThemeColors

CSS color palette. All fields are validated against injection attacks on construction.

```python
from hyperdjango.site_config import ThemeColors

theme = ThemeColors(
    primary="#ff6600",
    primary_dark="#e55b00",
    background="#f6f6ef",
    surface="#ffffff",
    text="#333333",
    text_secondary="#6b7280",
    border="#e5e7eb",
    success="#22c55e",
    warning="#f59e0b",
    danger="#dc2626",
    link="#1a73e8",
)
```

**Fields:** `primary`, `primary_dark`, `background`, `surface`, `text`, `text_secondary`, `border`, `success`, `warning`, `danger`, `link`

**Accepted formats:** `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()`, `rgba()`, `hsl()`, `hsla()`, named colors (`red`, `transparent`, etc.)

Invalid values (containing `<`, `>`, `;`, `{`, `}`) raise `ValueError` to prevent CSS injection.

#### to_css_vars()

Generates a CSS `:root` block with custom properties:

```python
css = theme.to_css_vars()
# :root {
#   --primary: #ff6600;
#   --primary-dark: #e55b00;
#   --background: #f6f6ef;
#   ...
# }

css = theme.to_css_vars(prefix="hn")
# :root {
#   --hn-primary: #ff6600;
#   --hn-primary-dark: #e55b00;
#   ...
# }
```

### SiteConfig

Base configuration class. Subclass with app-specific fields.

```python
from hyperdjango.site_config import SiteConfig

config = SiteConfig(
    name="My App",
    tagline="The best app",
    logo_icon="M",
    logo_url="/static/logo.png",
    favicon_url="/static/favicon.ico",
    footer_text="(c) 2026 My Company",
    font_family="Inter, system-ui, sans-serif",
    base_font_size="14px",
)
```

**Fields:**

| Field            | Type          | Default                          | Description                                |
| ---------------- | ------------- | -------------------------------- | ------------------------------------------ |
| `name`           | `str`         | `"HyperDjango"`                  | App name shown in title, nav, etc.         |
| `tagline`        | `str`         | `""`                             | Subtitle / description                     |
| `logo_icon`      | `str`         | `""`                             | Single character or emoji for compact logo |
| `logo_url`       | `str`         | `""`                             | URL to logo image                          |
| `favicon_url`    | `str`         | `""`                             | URL to favicon                             |
| `footer_text`    | `str`         | `"Powered by HyperDjango"`       | Footer attribution text                    |
| `theme`          | `ThemeColors` | default palette                  | Color theme                                |
| `font_family`    | `str`         | `"Inter, system-ui, sans-serif"` | CSS font-family                            |
| `base_font_size` | `str`         | `"14px"`                         | CSS base font size                         |

---

## Subclassing for App-Specific Config

Define a frozen dataclass that extends `SiteConfig`:

```python
from dataclasses import dataclass, field
from hyperdjango.site_config import SiteConfig, ThemeColors


@dataclass(frozen=True, slots=True)
class MyAppTheme(ThemeColors):
    """Custom color palette with extra fields."""

    primary: str = "#ff6600"
    accent: str = "#5a9e6f"  # app-specific extra color


@dataclass(frozen=True, slots=True)
class NavItem:
    url: str
    label: str


@dataclass(frozen=True, slots=True)
class MyAppConfig(SiteConfig):
    name: str = "My App"
    theme: MyAppTheme = field(default_factory=MyAppTheme)
    nav_items: tuple[NavItem, ...] = field(
        default_factory=lambda: (
            NavItem("/", "Home"),
            NavItem("/about", "About"),
        )
    )
    max_items_per_page: int = 25
```

---

## Loading from TOML

Use `load_site_config()` to load configuration from TOML files with environment variable overrides:

```python
from pathlib import Path
from hyperdjango.site_config import load_site_config

config = load_site_config(
    MyAppConfig,
    env_prefix="MYAPP",
    config_path=Path("site.toml"),
)
```

### Resolution Order (highest wins)

1. **Environment variables** (`MYAPP_NAME`, `MYAPP_THEME_PRIMARY`, etc.)
2. **TOML file** (if provided)
3. **Dataclass defaults**

### TOML File Format

```toml
# site.toml
name = "Custom Brand"
tagline = "Better than the rest"
logo_icon = "C"
footer_text = "Powered by Custom Brand"

[theme]
primary = "#3b82f6"
primary_dark = "#1d4ed8"
background = "#f0f4f8"
```

### Environment Variable Override

Environment variables use the pattern `{PREFIX}_{FIELD_NAME}`:

```bash
export MYAPP_NAME="Override Name"
export MYAPP_THEME_PRIMARY="#ff0000"
export MYAPP_MAX_ITEMS_PER_PAGE=50
```

Nested dataclass fields use double prefix: `MYAPP_THEME_PRIMARY` for `config.theme.primary`.

Only simple scalar types (`str`, `int`, `float`, `bool`) can be set via env vars. Complex types (`tuple`, nested dataclasses) must use TOML or Python construction.

---

## Template Integration

When `HyperApp(site_config=...)` is set, two template globals are auto-injected:

### {{ site }}

The full `SiteConfig` instance. Access any field:

```html
<title>{{ site.name }} - {{ page_title }}</title>
<span class="logo">{{ site.logo_icon }}</span>
<p>{{ site.footer_text }}</p>

{% for nav in site.nav_items %}
<a href="{{ nav.url }}">{{ nav.label }}</a>
{% endfor %}
```

### {{ site_css }}

Pre-generated CSS custom properties from `ThemeColors.to_css_vars()`. Include in `<head>`:

```html
<link rel="stylesheet" href="/static/style.css" />
{% if site_css %}
<style>
  {{ site_css }}
</style>
{% endif %}
```

The `<style>` tag MUST appear AFTER the `<link>` stylesheet so custom properties override defaults.

Use the variables in your CSS:

```css
.header { background: var(--primary); }
.card { background: var(--surface); border: 1px solid var(--border)); }
.danger-btn { background: var(--danger); }
```

---

## Per-Tenant Overlay

For multi-tenant deployments where each tenant has custom branding, use `dataclasses.replace()` to create modified copies without mutating the base config:

```python
import dataclasses

base_config = load_site_config(MyAppConfig, "MYAPP")

# Tenant-specific overlay
tenant_config = dataclasses.replace(
    base_config,
    name="Tenant Corp",
    footer_text="Powered by Tenant Corp",
    theme=dataclasses.replace(base_config.theme, primary="#0066cc"),
)
```

All `SiteConfig` instances are frozen (immutable), so overlays are always safe to share across threads.

---

## Real-World Example: HyperNews

The HyperNews service demonstrates a full white-label setup:

```
services/hypernews/
  config.py           # HyperNewsSiteConfig with 33 fields
  site.toml.example   # Sample TOML for customization
  app.py              # HyperApp(site_config=_site_config)
  templates/base.html  # {{ site.name }}, {{ site_css }}, etc.
```

**config.py** defines:

- `HyperNewsTheme(ThemeColors)` with HN-inspired colors + extra `mod_badge`, `accent_gray`
- `NavItem` for navigation links
- `EscalationConfig` for auto-moderation thresholds
- `HyperNewsSiteConfig(SiteConfig)` with nav items, footer links, rate limits, escalation rules

**Templates** use `{{ site.name }}` everywhere instead of hardcoded "HyperNews" — zero template changes needed to rebrand.

```bash
# Rebrand HyperNews via env vars alone:
HYPERNEWS_NAME="TechForum" \
HYPERNEWS_LOGO_ICON="T" \
HYPERNEWS_THEME_PRIMARY="#3b82f6" \
HYPERNEWS_FOOTER_TEXT="Powered by TechForum" \
uv run hyper start --app services.hypernews.app:app
```

---

## CSS Security

All `ThemeColors` fields are validated via regex on construction (`__post_init__`). Values containing HTML/CSS injection characters (`<`, `>`, `;`, `{`, `}`, `"`, `'`) are rejected with `ValueError`. This prevents XSS via color values like `</style><script>alert(1)</script>`.

Only valid CSS color formats are accepted:

- Hex: `#rgb`, `#rrggbb`, `#rrggbbaa`
- Functions: `rgb()`, `rgba()`, `hsl()`, `hsla()`
- Named: `red`, `blue`, `transparent`, `inherit`, etc.
