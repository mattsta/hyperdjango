"""
File-based routing conventions.

Documents and validates the naming conventions used by file-based routing.
"""

# Supported dynamic parameter patterns
PARAM_TYPES = {
    "id": "int",
    "pk": "int",
    "slug": "slug",
    "uuid": "uuid",
    "year": "int",
    "month": "int",
    "day": "int",
}

# Reserved filenames
RESERVED_NAMES = {"__init__", "__pycache__", "conftest"}

# Default view directory name
DEFAULT_VIEWS_DIR = "views"
