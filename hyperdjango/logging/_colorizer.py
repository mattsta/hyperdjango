"""
ANSI color markup parser and format engine.

Parses color tags like <red>, <bold>, <fg 255>, <bg #FF0000> in format
strings and messages, converting them to ANSI escape sequences.

Supports:
- Named colors: <red>, <green>, <blue>, <cyan>, <magenta>, <yellow>, <white>, <black>
- Light variants: <light-red>, <light-green>, etc.
- Styles: <bold>, <dim>, <italic>, <underline>, <strike>, <blink>, <reverse>
- 8-bit colors: <fg 196>, <bg 42>
- Hex colors (24-bit): <fg #FF5500>, <bg #00FF00>
- RGB colors: <fg 255,128,0>, <bg 0,255,0>
- Reset: </> or </color> or </bold> etc.
- Level tag: <level> (replaced with level's configured color)

Usage:
    from hyperdjango.logging._colorizer import colorize, strip_markup

    # Apply colors
    output = colorize("<red>Error:</red> <bold>{message}</bold>", level_color="\\033[31m")

    # Strip all markup (for non-tty output)
    plain = strip_markup("<red>Error:</red> message")
    # => "Error: message"
"""

import re

# ---------------------------------------------------------------------------
# ANSI escape codes
# ---------------------------------------------------------------------------

# Named colors (foreground)
_NAMED_FG = {
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    # Light variants
    "light-black": "\033[90m",
    "light-red": "\033[91m",
    "light-green": "\033[92m",
    "light-yellow": "\033[93m",
    "light-blue": "\033[94m",
    "light-magenta": "\033[95m",
    "light-cyan": "\033[96m",
    "light-white": "\033[97m",
}

# Named colors (background)
_NAMED_BG = {
    "black": "\033[40m",
    "red": "\033[41m",
    "green": "\033[42m",
    "yellow": "\033[43m",
    "blue": "\033[44m",
    "magenta": "\033[45m",
    "cyan": "\033[46m",
    "white": "\033[47m",
    "light-black": "\033[100m",
    "light-red": "\033[101m",
    "light-green": "\033[102m",
    "light-yellow": "\033[103m",
    "light-blue": "\033[104m",
    "light-magenta": "\033[105m",
    "light-cyan": "\033[106m",
    "light-white": "\033[107m",
}

# Style codes
_STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "underline": "\033[4m",
    "blink": "\033[5m",
    "reverse": "\033[7m",
    "strike": "\033[9m",
    "strikethrough": "\033[9m",
}

RESET = "\033[0m"

# Regex to match color tags: <tag> or </tag> or </>
_TAG_PATTERN = re.compile(
    r"<(/?)("
    r"[a-z][\w-]*"  # Named: <red>, <bold>, <light-cyan>
    r"|fg\s+\d+"  # 8-bit fg: <fg 196>
    r"|bg\s+\d+"  # 8-bit bg: <bg 42>
    r"|fg\s+#[0-9a-fA-F]{3,6}"  # Hex fg: <fg #FF5500>
    r"|bg\s+#[0-9a-fA-F]{3,6}"  # Hex bg: <bg #00FF00>
    r"|fg\s+\d+,\d+,\d+"  # RGB fg: <fg 255,128,0>
    r"|bg\s+\d+,\d+,\d+"  # RGB bg: <bg 0,255,0>
    r"|level"  # Level color: <level>
    r"|/?"  # Close all: </>
    r")>",
)

# ---------------------------------------------------------------------------
# Tag to ANSI resolver
# ---------------------------------------------------------------------------


def _resolve_tag(tag: str, level_color: str = "") -> str:
    """Convert a tag name to its ANSI escape sequence."""
    tag = tag.strip().lower()

    # Level color
    if tag == "level":
        return level_color

    # Named color (foreground)
    if tag in _NAMED_FG:
        return _NAMED_FG[tag]

    # Style
    if tag in _STYLES:
        return _STYLES[tag]

    # 8-bit foreground: "fg 196"
    if tag.startswith("fg "):
        val = tag[3:].strip()
        if val.startswith("#"):
            return _hex_to_ansi_fg(val)
        if "," in val:
            return _rgb_to_ansi_fg(val)
        try:
            return f"\033[38;5;{int(val)}m"
        except ValueError:
            return ""

    # 8-bit background: "bg 42"
    if tag.startswith("bg "):
        val = tag[3:].strip()
        if val.startswith("#"):
            return _hex_to_ansi_bg(val)
        if "," in val:
            return _rgb_to_ansi_bg(val)
        try:
            return f"\033[48;5;{int(val)}m"
        except ValueError:
            return ""

    return ""


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert hex color (#RGB or #RRGGBB) to (r, g, b)."""
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r, g, b


def _hex_to_ansi_fg(hex_str: str) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"\033[38;2;{r};{g};{b}m"


def _hex_to_ansi_bg(hex_str: str) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"\033[48;2;{r};{g};{b}m"


def _rgb_to_ansi_fg(val: str) -> str:
    parts = val.split(",")
    if len(parts) != 3:
        return ""
    try:
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        return f"\033[38;2;{r};{g};{b}m"
    except ValueError:
        return ""


def _rgb_to_ansi_bg(val: str) -> str:
    parts = val.split(",")
    if len(parts) != 3:
        return ""
    try:
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        return f"\033[48;2;{r};{g};{b}m"
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def colorize(text: str, level_color: str = "") -> str:
    """Apply color markup tags in text, converting to ANSI escape sequences.

    Tags like <red>, <bold>, <fg 255>, </> are replaced with ANSI codes.
    <level> is replaced with the level's configured color.
    </tag> or </> resets to default.
    """

    def _replace(match):
        is_close = match.group(1) == "/"
        tag = match.group(2)

        if is_close or tag == "/":
            return RESET
        return _resolve_tag(tag, level_color)

    return _TAG_PATTERN.sub(_replace, text)


def strip_markup(text: str) -> str:
    """Remove all color markup tags from text, leaving plain text."""
    return _TAG_PATTERN.sub("", text)


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from text."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def colorize_format(format_str: str, level_color: str = "") -> str:
    """Colorize a format template string (before format_map is applied).

    This is called once per handler per level to precompile the colorized
    format string, avoiding re-parsing on every log call.
    """
    return colorize(format_str, level_color)


def decolorize_format(format_str: str) -> str:
    """Strip color markup from a format template string.

    Used for non-tty sinks that don't support ANSI codes.
    """
    return strip_markup(format_str)
