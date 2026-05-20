"""
Team, membership, and agent skill models.

Teams group agents into departments/queues. Each team has a lead.
AgentSkill maps agent proficiency to tags for skill-based routing.
"""

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.tenancy import TenantMixin

from .users import Agent


class Team(TenantMixin, TimestampMixin, Model):
    """Department or queue that groups agents."""

    class Meta:
        table = "ht_teams"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field()
    slug: str = Field()
    description: str = Field(default="")
    lead_agent_id: int = Field(default=0)  # FK to Agent (0 = no lead)
    is_active: bool = Field(default=True)


class TeamMembership(TenantMixin, TimestampMixin, Model):
    """Agent membership in a team. An agent can belong to multiple teams."""

    class Meta:
        table = "ht_team_memberships"

    id: int = Field(primary_key=True, auto=True)
    team_id: int = Field(foreign_key=Team)
    agent_id: int = Field(foreign_key=Agent)
    is_primary: bool = Field(default=False)  # agent's primary team


class AgentSkill(TenantMixin, TimestampMixin, Model):
    """Agent proficiency on a skill tag. Used for skill-based ticket routing."""

    class Meta:
        table = "ht_agent_skills"

    id: int = Field(primary_key=True, auto=True)
    agent_id: int = Field(foreign_key=Agent)
    skill_tag: str = Field()  # matches Tag.name for routing
    proficiency: int = Field(default=3)  # 1-5 scale
