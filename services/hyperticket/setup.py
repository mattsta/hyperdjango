"""
HyperTicket — Post-setup configuration.

Table creation and indexes are handled automatically by `hyper setup --app`
reading Model definitions and Meta.indexes.

Usage:
    uv run hyper setup --app services.hyperticket.app:app
    uv run hyper setup --app services.hyperticket.app:app --seed services.hyperticket.seed:run
"""
