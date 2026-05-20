# HyperTicket

Production-grade multi-tenant SaaS ticketing system showcasing every major HyperDjango platform feature: multi-tenancy with TenantMixin, guard-based access control with intent resolvers, BaseModel input validation, dual auth flows (agent + customer portals), HTMX-powered UI, background tasks, realtime WebSocket notifications, metering/quotas, SLA tracking, workflow configuration, and HyperAdmin with all 27+ models registered.

## Features

- Multi-tenant isolation (TenantMixin, TenantMiddleware, per-org data)
- Guard-based access control with role hierarchy (admin, team_lead, agent)
- Dual auth flows: agent login + customer register/login portals
- Configurable workflows: statuses, priorities, types, transitions per org
- SLA policies with breach escalation rules
- Ticket operations: create, assign, close, reopen, merge, split, lock, mute
- Comments with internal/public visibility
- Tags, attachments, canned responses, ticket templates
- Kanban board view and saved views
- Full-text search across tickets and comments
- CSV/JSON ticket export
- Agent and team analytics dashboards
- Approval workflows for sensitive actions
- Org-scoped API keys (SignedAPIKeyMixin)
- StatusTimelineMixin for ticket lock/mute and agent lifecycle
- Custom fields per org
- Ticket relations (parent/child, duplicate, related)
- Bulk update operations
- CSAT (customer satisfaction) ratings
- Native telemetry with Prometheus metrics endpoint
- CSRF, CORS, rate limiting, security headers middleware
- HyperAdmin panel with all models, fieldsets, actions, and filters

## Setup

```bash
cd services/hyperticket
uv run hyper setup --app app:app --drop --seed seed:run
uv run hyper start --app app:app --port 18810
```

## Credentials

Seed creates 2 organizations with agents and customers. Passwords are not
hardcoded — they resolve from `HYPER_SEED_PASSWORD_<USERNAME>` /
`HYPER_SEED_PASSWORD` (per-username falls back to global) and
`HYPER_ADMIN_PASSWORD` for the HyperAdmin panel user, or are generated
randomly and printed to the seed log if neither is set.

**Acme Corp** (Professional plan):

- Agent admin: `admin@acme.com` (role: admin)
- Agent: `alice@acme.com` (role: team_lead)
- Agent: `bob@acme.com` (role: agent)
- Agent: `carol@acme.com` (role: agent)
- Customers: `cust1@example.com`, `cust2@example.com`, `cust3@example.com`

**Globex Inc** (Enterprise plan):

- Agent admin: `admin@globex.com` (role: admin)
- Agent: `grace@globex.com` (role: team_lead)
- Agent: `henry@globex.com` (role: agent)
- Customers: `client1@partner.com`, `client2@partner.com`

Set `HYPER_SEED_PASSWORD=<value>` and `HYPER_ADMIN_PASSWORD=<value>` before
seeding to use a known dev password across all seeded users.

## Key Routes

**Auth**

- `GET /auth/agent/login` -- Agent login page
- `POST /auth/agent/login` -- Agent login (email + password + org_slug)
- `GET /auth/customer/register` -- Customer registration page
- `POST /auth/customer/register` -- Customer registration
- `GET /auth/customer/login` -- Customer login page

**Agent Portal**

- `GET /tickets/` -- Ticket list (cursor-paginated, filterable)
- `GET /tickets/new` -- Create ticket form
- `GET /tickets/{id}` -- Ticket detail with comments, timeline, tags
- `POST /tickets/{id}/assign` -- Assign agent/team
- `POST /tickets/{id}/close` -- Close ticket
- `POST /tickets/{id}/reopen` -- Reopen ticket
- `POST /tickets/{id}/merge` -- Merge into another ticket
- `POST /tickets/{id}/split` -- Split into new ticket
- `POST /tickets/{id}/comments` -- Add comment (public or internal)
- `GET /tickets/{id}/timeline` -- Activity timeline
- `GET /board/` -- Kanban board view
- `GET /search/` -- Full-text search

**Customer Portal**

- `GET /portal/tickets/` -- Customer's tickets
- `GET /portal/tickets/new` -- Submit a ticket
- `POST /portal/tickets/{id}/comment` -- Add comment
- `POST /portal/tickets/{id}/rate` -- CSAT rating (1-5)

**Management**

- `GET /agents/` -- Agent list
- `GET /teams/` -- Team list
- `GET /tags/` -- Tag list
- `GET /dashboard/` -- Overview dashboard
- `GET /analytics/agents/` -- Agent performance analytics
- `GET /analytics/teams/` -- Team analytics
- `GET /analytics/volume/` -- Ticket volume analytics
- `GET /tickets/export/` -- Export tickets (CSV/JSON)
- `GET /approvals/` -- Pending approvals
- `GET /saved-views/` -- Saved filter views
- `GET /templates/` -- Ticket templates
- `GET /canned-responses/` -- Canned response library

**Admin**

- `GET /admin/` -- HyperAdmin panel (all 27+ models)
- `GET /admin/settings/` -- Org settings
- `GET /admin/api-keys/` -- API key management
- `GET /admin/custom-fields/` -- Custom field configuration

**Infrastructure**

- `GET /health` -- Liveness probe
- `GET /ready` -- Readiness probe
- `GET /metrics` -- Prometheus metrics

## Seed Data

- 3 plans: Starter, Professional, Enterprise (with feature limits)
- 2 orgs: Acme Corp (10 tickets), Globex Inc (8 tickets)
- Workflow configs per org: 7 statuses, 4 priorities, 5 ticket types, 15 transitions
- Teams, tags, agent skills, SLA policies, escalation rules
- Sample tickets with comments and tags
