# HyperNews

A production-grade community platform built with HyperDjango. Demonstrates multi-forum architecture, threaded comments, karma-weighted voting, eigenvector ring detection, session auth, HTMX interactivity, and 15 social features across 4 phases — all backed by native Zig performance.

7,000+ lines of application code. 100 routes. 44 templates. 28 validation schemas. 295 E2E tests.

## Quick Start

```bash
uv run hyper-build --install
uv run hyper setup --app services.hypernews.app:app --seed services.hypernews.setup:run
uv run hyper run --app services.hypernews.app:app --port 8000
# Open http://localhost:8000
# Seed users: admin, alice, bob — passwords from HYPER_SEED_PASSWORD env var or printed to seed log
```

## Architecture

```
services/hypernews/
  app.py          5,500 lines — 100 routes, 28 validation schemas, purpose-based access control
  models.py         377 lines — 20+ models (Forum, Post, Comment, Vote, Poll, Award, etc.)
  voting.py         984 lines — 6-phase voting, trust tiers, eigenvector centrality, meta-moderation
  setup.py          231 lines — seed data (3 users, 8 forums, sample posts)
  templates/         44 files — full HTML UI with HTMX interactivity
  static/style.css        CSS — responsive orange theme
```

## Features by Phase

### P0: Multi-Forum Architecture (76 E2E tests)

Subreddit-style communities with per-forum moderation.

| Feature         | Implementation                                                           |
| --------------- | ------------------------------------------------------------------------ |
| Forum CRUD      | Create (karma-gated), edit settings, soft-delete (hide+archive)          |
| Membership      | Join/leave, subscriber_count denormalized with background reconciliation |
| Roles           | ForumRole enum: subscriber, moderator, admin — per-forum, not global     |
| Privacy         | Public/private forums, hidden forums (members-only visibility)           |
| States          | Archived (read-only), locked (no new posts), hidden (unlisted)           |
| Admin transfer  | Transfer admin role with last-admin protection                           |
| Audit logging   | All mod actions logged with actor, target, reason, timestamp             |
| Forum directory | Browsable, searchable, sorted by subscriber count                        |
| Post scoping    | Every post belongs to a forum (forum_id FK), global posts supported      |

**Access control:** `resolve_forum(request, name, ForumIntent.WRITE_POST)` — purpose-based resolver with 5 intents (READ, WRITE_POST, WRITE_COMMENT, MODERATE, ADMIN). One call replaces 10+ lines of manual checks.

### P1: User Experience (40 E2E tests)

| Feature          | Implementation                                                            |
| ---------------- | ------------------------------------------------------------------------- |
| Bookmarks        | Toggle bookmark on posts/comments, `/saved` page with type filters        |
| Notifications    | Reply/mention triggers, `/inbox` with unread count (HTMX badge, 30s poll) |
| User profiles    | Extended: bio, website, location, avatar, GitHub link, karma breakdown    |
| Profile settings | Edit all profile fields via `/settings/profile`                           |

### P2: Content Features (43 E2E tests)

| Feature      | Implementation                                                      |
| ------------ | ------------------------------------------------------------------- |
| Drafts       | Save as draft, publish later, `/drafts` management page             |
| Post editing | Edit with revision history, diff viewer at `/post/{pid}/history`    |
| Polls        | Single/multiple choice, vote toggle, inline results with bar charts |
| Crossposting | Crosspost to any forum with attribution and dedup protection        |
| User flairs  | Per-forum, self-assign or mod-assign, CSS class whitelist           |
| RSS feeds    | Global, per-forum, per-user — RSS 2.0 via syndication module        |

### P3: Platform Features (31 E2E tests)

| Feature         | Implementation                                                       |
| --------------- | -------------------------------------------------------------------- |
| Content pinning | Mod/admin pin up to 3 posts per forum, audit logged                  |
| Sequences       | Ordered post collections (LessWrong-style series), table of contents |
| Post awards     | 4 types (insightful, well_written, helpful, funny), karma-gated      |
| Related posts   | Score-range similarity within same forum                             |
| Automod rules   | CRUD, BaseModel-validated conditions, auto-remove/flag/notify        |
| Mod dashboard   | Stats, recent actions, quick links at `/f/{name}/mod`                |

### Core (pre-P0)

| Feature               | Implementation                                                    |
| --------------------- | ----------------------------------------------------------------- |
| Threaded comments     | Depth-tracked, parent_id FK, recursive rendering                  |
| Karma + trust tiers   | Weighted votes, tier-based privileges (TrustTier model)           |
| Time-decay ranking    | 5 sort modes: hot (60s refresh), new, top, controversial, rising  |
| Keyset pagination     | HMAC-signed cursors on all tabs including search                  |
| Moderation queue      | SpamReport, ModAction pipeline, mod notes, bulk actions           |
| Rate limiting         | Per-user vote rate, rapid-fire detection, SecurityLog             |
| Voting ring detection | Eigenvector centrality, reciprocity analysis, community detection |
| Full-text search      | PostgreSQL tsvector with keyset-paginated results                 |
| Ask HN / Show HN      | Post type detection from title prefix                             |

## Platform Features Demonstrated

| Platform Feature        | How HyperNews Uses It                                                |
| ----------------------- | -------------------------------------------------------------------- |
| Native Zig server       | 24-thread HTTP server, sub-ms response times                         |
| BaseModel validation    | 28 schema classes via `validate_form()`, Zig-accelerated             |
| Session auth (Argon2id) | Login/register/logout with CSRF protection                           |
| Keyset pagination       | HMAC-signed cursors on all listing pages (5 sort tabs)               |
| HyperAdmin              | Auto-CRUD for all 20+ models, custom ban/mute/delete actions         |
| HTMX                    | Voting, commenting, reply forms, bookmark toggle, inbox badge        |
| Background tasks        | Hot score refresh (60s), count reconciliation (10m), cleanup         |
| Full-text search        | PostgreSQL tsvector on posts and forums                              |
| Rate limiting           | Per-user vote rate, rapid-fire detection, per-action throttles       |
| SecurityLog             | Brute force protection, voting ring detection                        |
| RSS/Atom                | Feed generation via `hyperdjango.syndication`                        |
| LocMemCache             | 10s TTL on listing pages, 30s on visibility sets, 2s on user objects |

## Security Model

- **Purpose-based access control**: `resolve_forum(ForumIntent.X)` and `resolve_post(require_author=, require_not_locked=)` centralize all checks
- **Private forum isolation**: Enforced on ALL code paths — front page, search, post detail, vote, comment, report, bookmark, awards
- **BaseModel validation**: All 31 form-input routes use schema validation (no manual isinstance/key checks)
- **IntegrityError handling**: Race conditions on join/register/bookmark caught via DB constraint violations
- **Ban/mute enforcement**: All write routes use `@require_active_user` which checks ban/mute status per-request
- **CSRF**: All POST routes protected via CSRFMiddleware, logout is POST with CSRF token
- **Rate limiting**: Vote (30/min), comment (10/min), submit (3/min), forum create (10/hr), awards (5/min)
- **Voting ring detection**: Eigenvector centrality analysis, reciprocity scoring, community detection
- **Parameterized queries**: All forum visibility filters use `ANY($N::int[])`, no string interpolation

## Routes (100 total)

**Forum:** `/f/{name}/`, `/f/{name}/about`, `/f/{name}/edit`, `/f/{name}/submit`, `/f/{name}/members`, `/f/{name}/join`, `/f/{name}/leave`, `/f/{name}/mod`, `/f/{name}/mod/appoint`, `/f/{name}/mod/remove`, `/f/{name}/mod/transfer`, `/f/{name}/delete`, `/f/{name}/audit`, `/f/{name}/flair`, `/f/{name}/pin/{pid}`, `/f/{name}/automod`, `/f/{name}/feed/rss`

**Posts:** `/submit`, `/post/{pid}`, `/post/{pid}/edit`, `/post/{pid}/history`, `/post/{pid}/publish`, `/post/{pid}/poll`, `/post/{pid}/crosspost`, `/post/{pid}/award`, `/post/{pid}/related`

**Social:** `/vote`, `/comment`, `/bookmark`, `/report`, `/agree`, `/tag`, `/mod/note`, `/award`, `/inbox`, `/saved`, `/drafts`, `/messages`, `/settings/profile`

**Discovery:** `/`, `/forums`, `/forums/create`, `/forums/search`, `/search`, `/sequences`, `/user/{username}`, `/feed/rss`

**Auth:** `/login`, `/register`, `/logout`, `/account`

**Analytics:** `/analytics/rings`, `/analytics/domains`, `/analytics/centrality`, `/analytics/communities`, `/analytics/reconcile-counts`, `/analytics/count-drift`

## Tests

```bash
uv run hyper-test e2e_hypernews           # 17 base tests
uv run hyper-test e2e_hypernews_forums    # 76 forum tests
uv run hyper-test e2e_hypernews_social    # 40 social tests
uv run hyper-test e2e_hypernews_p2        # 43 P2 tests
uv run hyper-test e2e_hypernews_p3        # 31 P3 tests
uv run hyper-test e2e_hypernews_workflow  # 32 workflow tests
uv run hyper-test e2e_voting_system       # 56 voting tests
# Total: 295 E2E tests
```

## Project Structure

```
hypernews/
  app.py                    Routes, middleware, admin, validation schemas, access control
  models.py                 20+ models: User, Post, Comment, Vote, Forum, ForumMember,
                            Bookmark, Notification, UserProfile, PostRevision, Poll,
                            PollOption, PollVote, UserFlair, Award, Sequence,
                            SequenceEntry, AutomodRule, SpamReport, AdminMessage
  voting.py                 6-phase voting engine, trust tiers, graph analytics,
                            meta-moderation (agree/disagree), mod notes, content tags
  setup.py                  Seed script: 3 users, 8 forums, sample posts
  templates/
    base.html               Layout: orange header, nav (forums/inbox/saved), HTMX, footer
    index.html              Front page: aggregated feed with 5 sort tabs
    forums.html             Forum directory with search
    forum_home.html         Forum page: posts, sidebar, join/leave, mod panel link
    forum_about.html        Forum info: description, rules, mod list, stats
    forum_edit.html         Admin settings: title, description, rules, public/archived/locked/hidden
    forum_submit.html       Post submission within a forum
    forum_members.html      Member list with role badges
    create_forum.html       Forum creation form (karma-gated)
    submit.html             Global post submission with forum picker
    post_detail.html        Post view: voting, poll, awards, crosspost badge, related posts,
                            pin controls, edit history link, threaded comments
    post_history.html       Revision diff viewer
    poll_create.html        Poll creation: question, type, options
    crosspost.html          Crosspost form: forum selector, preview
    award_give.html         Award form: type selector, karma cost
    search.html             Search results with keyset pagination
    user_profile.html       Profile: avatar, bio, karma, memberships, recent posts/comments
    profile_settings.html   Edit profile: all extended fields
    account.html            Account settings: display name, bio, email, password
    login.html              Login form with brute force protection
    register.html           Registration with username/password validation
    inbox.html              Notifications with unread state, mark-all-read
    saved.html              Bookmarks with type filter tabs (all/posts/comments)
    drafts.html             Draft management with preview
    messages.html           Admin messages inbox
    sequences.html          Browse all sequences
    sequence_detail.html    Sequence TOC: ordered posts, add/remove for author
    sequence_create.html    New sequence form
    flair_manage.html       Self-assign + mod flair management
    automod_manage.html     Rule list + creation form for admins
    mod_panel.html          Mod dashboard: stats, recent actions, quick links
    mod_audit_log.html      Audit log viewer: paginated action history
    mod_actions.html        Per-user mod action history
    404.html / 500.html     Error pages
    _partials/
      post_item.html        Post row: vote, title, domain, meta, pin badge
      comment.html          Recursive threaded comment with depth tracking
      vote_button.html      Reusable vote controls (post or comment)
      poll_widget.html      Inline poll: options, vote buttons, bar charts
      awards_badge.html     Grouped award icons with counts
      flair_badge.html      Inline flair display next to username
      pin_badge.html        Pinned indicator
      crosspost_badge.html  "Crossposted from /f/X" banner
      related_posts.html    Related posts sidebar section
  static/
    style.css               Responsive orange theme, poll/flair/award/pin/sequence CSS
```
