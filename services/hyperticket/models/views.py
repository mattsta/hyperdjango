"""
Saved view model — per-agent or shared filtered ticket views.
"""

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin


class SavedView(TenantMixin, TimestampMixin, Model):
    """Saved filter/sort preset for the ticket list.

    agent_id=0 means shared view (visible to all agents in org).
    """

    class Meta:
        table = "ht_saved_views"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    agent_id: int = Field(default=0)  # FK to Agent (0 = shared)
    filter_criteria: str = Field(default="{}")  # JSON filter definition
    sort_order: str = Field(default="-created_at")
    is_default: bool = Field(default=False)
    columns: str = Field(default="[]")  # JSON array of visible column names
