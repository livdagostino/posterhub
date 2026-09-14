<p align="center">
  <img
    src="bot_engine/static/images/posterhub.png"
    alt="PosterHub"
    width="100%">
</p>

<br>

# PosterHub: Research Paper Assistant

A Django web application for building a searchable, AI-powered library of scientific research posters and papers. Upload a poster via the web, Telegram, or WhatsApp: the system extracts metadata, finds the published paper, searches for the code repository, and saves everything in a structured dashboard.

---

## Features

### Multi-Channel Upload
- **Web** -- drag-and-drop upload with progress bar, real-time AI analysis status, and multi-image batch upload
- **Telegram bot** -- send a photo or document to receive structured results
- **WhatsApp bot** -- same workflow, with interactive button prompts

### Research Groups
- Each paper can be assigned to one or more research groups; the dashboard, donut stats, and exports are always scoped to the groups the user belongs to (no cross-group leakage)
- One **primary group** per user, used to pre-select chips on upload and to drive the per-group "Why useful" generation
- **Per-paper, per-group group selection** at upload time: in multi-file batches each paper has its own group chips so different posters can land in different groups within the same submission
- **Group editor on paper detail**: pencil button next to the Groups badges opens an inline panel with the user's groups; toggles only the user's memberships and preserves any other group's link to the paper
- **Mine** filter: dashboard pill that scopes the table and the donut to papers the current user uploaded
- **Group management UI** (`/groups/`, group-manager or admin): create/edit/delete groups, add/remove members, set primary, and a "users awaiting group assignment" panel with one-click add-to-group plus a **Skip** action that suppresses the alert for users you do not want to assign
- **My Groups** page (`/my-groups/`): each user can pick which of their groups is primary; group managers and admins can also open group and research-interest editing directly
- **Manage Groups** is available to group managers and admins from Upload, Dashboard, Conference and My Groups, including when they have no research-group membership. **Users** remains admin-only

### Access Control & Roles

The platform has three role levels:

| Role | How it's set | Capabilities |
|---|---|---|
| **Admin** | `is_superuser=True` (set via `/users/` or Django admin) | Full access: manage users, manage groups, edit any paper, all AJAX endpoints |
| **Group Manager** | Member of the Django auth group `GestoreGruppi` | Manage research groups (create/edit/delete, add/remove members, manage research interests) and dismiss pending users — but **cannot** manage users or change roles |
| **User** | Default | Can use the platform only when added to a research group; otherwise sees a banner and locked screens |

- **Shibboleth SSO** integration for production (`SHIBBOLETH_AUTH=true`); auto-creates Django users from `HTTP_X_SHIB_*` headers and signs them in transparently
- **Restricted mode for unassigned users**: a logged-in account that is not in any research group can reach the site but the dashboard is replaced with a "Dashboard locked" notice, the upload form is replaced with a placeholder, and every write/AJAX endpoint (upload, analyses, bulk actions, conference search, favorites/notes/tags) is blocked by the `_groups_required` decorator. Group managers still see a "Manage Groups" button on the locked screen
- **User admin** (`/users/`, admin-only): list active users with their groups and uploads; promote/demote admins; toggle group-manager role; delete accounts. Hard delete also removes the row from `auth_user` and from `/admin/auth/user/`

### AI-Powered Extraction (GPT-4o Vision)
From a single poster image the system extracts:
- Title, authors, abstract, keywords, category, conference, year, institution
- Automatic classification into: ML, CV, NLP, Robotics, HCI, Data Science, Theory, Systems
- Non-research image detection -- rejects photos that do not contain scientific content

### Paper and Code Search Pipeline

| Step | Source | Purpose |
|------|--------|---------|
| 1 | Visible arXiv ID and arXiv title search | Verified paper page, PDF, authors, abstract and year; title/acronym queries also handle different poster subtitles |
| 2 | arXiv web search | Independent fallback when the export API cannot supply a matching paper |
| 3 | Semantic Scholar, then Google Scholar | Additional sources, bounded retries, explicit HTTP/challenge logging, and title validation |
| 4 | Paper page / DOI / PDF search | Locate a usable PDF; recover the paper link from a PDF-only match |
| 5 | PDF annotations, PDF text, paper page, GitHub API | Find the code repository |
| 6 | Located GitHub project | Recover a missing paper from arXiv references, validating the referenced title |

arXiv metadata is cached (successful queries for 24 hours, empty results for five
minutes), with shared request spacing. Provider outages are not cached as missing
papers; a short cooldown lets other sources continue. Manual links survive
consecutive retries, and temporary lookup failures do not erase known links.
External sources can still be unavailable; logs distinguish HTTP failures from
searches that return no matching paper.

To preview recovery for specific existing records without rerunning AI or sending
bot messages, then apply only the missing verified links:

```bash
docker compose exec django python manage.py repair_paper_links 157 158
docker compose exec django python manage.py repair_paper_links 157 158 --apply
```

The command preserves existing links, summaries, notes, groups and validation
status, and skips records whose analysis is currently running.

### Dashboard
- Full table view: title, authors, category, tags, paper link, GitHub link, summary, notes
- Poster thumbnail in a unified "Links & Poster" column
- Advanced search: filter by author, description, date range
- Column sorting: clickable headers for ID/date, title, category, status
- Filter by status (Pending / Approved / Rejected), category, subfields, favorites, GitHub availability
- Inline notes: add/edit personal notes directly in the table row
- Bulk actions: select multiple papers to approve, reject, star, unstar, or delete
- Pagination: configurable page size (10 / 25 / 50 / 100)
- Stat tiles with live counters (total, pending, approved, rejected, favorites)
- Activity log with clear-all option
- `NEW` badge on papers uploaded in the last 24 hours
- Export approved papers as CSV or JSON

### Upload Page
- Drag-and-drop single or multiple images at once
- Multi-image batch upload: each image becomes a separate analysis task with its own group selection
- Single-file mode shows notes/tags fields plus a shared group chip row; multi-file mode renders one row per paper (thumbnail + filename + per-paper group chips), so different papers can be filed into different groups in the same submission
- Compact "drop more here" hint stays visible above the multi-file list so additional files can be added at any time
- Backend validates that every uploaded paper has at least one group selected, with a per-file error message when a paper is missing its assignment
- Collapsible tips panel for best upload results
- Collapsible recent uploads section showing the last 3 uploads with status
- Upload progress bar with real-time feedback

### Analysis Status & Retry
- **Processing** -- AI analysis running in background via Celery
- **OK** -- analysis completed successfully
- **Failed** -- AI error; poster is kept in DB with the original image
  - Retry button re-queues the analysis task
  - Stop button cancels a running analysis
- **Duplicate** -- poster discarded (detected by image hash), existing record kept
- **No text** -- non-scientific image, poster discarded

### Responsive Design
- Table rows become stacked cards on mobile
- All columns remain accessible on small screens
- Bulk bar, pagination, and advanced filters adapt to mobile layout
- Dark mode support

### Bot Commands

| Platform | Commands |
|----------|----------|
| Telegram | `/start`, `/link <email>`, `/help`, `/dashboard`, `/search <keyword>`, send photo/document |
| WhatsApp | `start`, `link <email>`, `help`, `dashboard`, `search <keyword>`, send photo/document |

Both bots support:
- Inline notes and tags via captions (e.g. `note: interesting  tag: deep learning, mri`)
- Year-filtered search (e.g. `/search transformer year:2024` or `/search segmentation year:2020-2024`)
- Upload tips and usage guidance via `/help`

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Django 6.x, Python 3.12, Gunicorn |
| Task Queue | Celery 5.4, Redis 7 |
| Database | MySQL 8.4 |
| AI | OpenAI GPT-4o (Vision) |
| Paper search | Semantic Scholar, Google Scholar, arXiv |
| Code search | GitHub API, pypdf, BeautifulSoup |
| Messaging | Telegram Bot API, WhatsApp Cloud API |
| Frontend | Django templates, Tailwind CSS (CDN), vanilla JS |
| Reverse Proxy | Apache HTTPD 2.4 |
| Containerization | Docker, Docker Compose |

---

## Architecture

```
                 +------------+
  Browser ------>|  Apache    |---- /media/ --> static files
  Telegram ----->|  (port 80) |---- /* ------> Gunicorn :8000
  WhatsApp ----->|            |                  |
                 +------------+                  |
                                                 v
                                          +------------+
                                          |  Django    |
                                          |  views.py  |
                                          +-----+------+
                                                | .delay()
                                     +----------+----------+
                                     v                     v
                              +------------+        +------------+
                              |   Celery   |        |   Celery   |
                              |   Worker   |        |   Beat     |
                              +------+-----+        +------------+
                                     |
                           +---------+---------+
                           v         v         v
                       +-------+ +-------+ +--------+
                       | MySQL | | Redis | | OpenAI |
                       +-------+ +-------+ +--------+
```

All media downloads from Telegram and WhatsApp are handled asynchronously via Celery to prevent webhook timeouts.

---

## Deployment

### Prerequisites
- Docker and Docker Compose
- OpenAI API key
- Telegram Bot token (from @BotFather)
- WhatsApp Cloud API credentials (Meta Business)
  - Use a **System User token** (permanent) -- temporary tokens expire every 24 hours

### 1. Clone the repository
```bash
git clone <your-repo-url>
cd PaperProject
```

### 2. Configure environment variables
Create a `.env` file in the project root:
```env
# Django
SECRET_KEY=your_django_secret_key
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com

# OpenAI
OPENAI_API_KEY=your_openai_api_key

# Telegram
BOT_TOKEN=your_telegram_bot_token
TELEGRAM_WEBHOOK_SECRET=your_random_secret

# WhatsApp (use System User token for permanent access)
WHATSAPP_TOKEN=your_whatsapp_permanent_token
WHATSAPP_PHONE_ID=your_whatsapp_phone_id
WHATSAPP_VERIFY_TOKEN=your_webhook_verify_token

# Semantic Scholar (optional, improves paper search)
SEMANTIC_SCHOLAR_API_KEY=your_key

# GitHub API (optional, higher rate limits)
GITHUB_TOKEN=your_github_token

# Shibboleth SSO (production only)
SHIBBOLETH_AUTH=true
```

### 3. Start all services
```bash
docker compose up --build -d
```

This starts five containers (plus the front Apache, when enabled):

| Container | Role |
|-----------|------|
| `posterhub-db` | MySQL 8.4 database |
| `posterhub-redis` | Redis 7 (Celery broker + cache) |
| `posterhub-django` | Gunicorn (3 workers, 120s timeout); runs `migrate` + `collectstatic` on entrypoint |
| `posterhub-worker` | Celery worker (async tasks) |
| `posterhub-beat` | Celery beat (scheduled tasks, persistent schedule) |

### 4. Configure bot webhooks
Set webhook URLs in each platform's dashboard:

- **Telegram** -- `https://yourdomain.com/telegram-webhook/`
- **WhatsApp** -- `https://yourdomain.com/whatsapp-webhook/`

### 5. Create a superuser (optional)
```bash
docker compose exec django python manage.py createsuperuser
```

---

## Project Structure

```
PaperProject/
├── apache/
│   ├── Dockerfile
│   └── httpd.conf
├── bot_engine/
│   ├── models.py              # ResearchPoster, ResearchGroup, UserGroupMembership,
│   │                          # PendingAssignmentDismissal, BotAccount, ...
│   ├── views.py               # Web views, group/user management, bot webhook handlers
│   ├── tasks.py               # Celery tasks (media download, AI analysis)
│   ├── middleware.py          # Shibboleth auth (auto-creates Django users from headers)
│   ├── access.py              # is_group_manager / user_can_interact helpers
│   ├── context_processors.py  # No-groups banner + role flags for templates
│   ├── utils_ai.py            # OpenAI integration, paper/code search
│   ├── prompts.py             # GPT-4o system prompts
│   ├── forms.py               # Upload and edit forms
│   ├── admin.py               # Django admin registrations (ResearchGroup, memberships, ...)
│   ├── migrations/
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/
│   └── templates/
│       ├── base.html
│       ├── dashboard.html
│       ├── _dashboard_partial.html
│       ├── _pagination_ajax.html            # AJAX pagination (dashboard)
│       ├── _pagination_links.html           # href-based pagination (users / groups)
│       ├── _subfields_grouped.html          # custom widget template
│       ├── upload.html
│       ├── poster_detail.html
│       ├── edit_poster.html
│       ├── conference.html
│       ├── my_groups.html
│       ├── login.html
│       ├── groups/            # /groups/ (group-manager + admin)
│       └── users/             # /users/ (admin only)
├── tesi_project/
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py
│   └── wsgi.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── manage.py
└── .env
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET/POST | Upload page (supports multi-image upload) |
| `/dashboard/` | GET | Dashboard with filters and search |
| `/dashboard/live-status/` | GET | Live processing status (AJAX) |
| `/poster/<id>/` | GET | Paper detail page |
| `/poster/<id>/update-groups/` | POST | Toggle the paper's group assignments (own groups only) |
| `/edit/<id>/` | GET/POST | Edit paper metadata |
| `/login/` | GET/POST | Login page |
| `/task-status/<task_id>/` | GET | Celery task status (AJAX) |
| `/retry-analysis/<id>/` | POST | Retry failed AI analysis |
| `/stop-analysis/<id>/` | POST | Stop running AI analysis |
| `/update-status/<id>/` | POST | Update paper validation status |
| `/toggle-favorite/<id>/` | POST | Toggle favorite/star |
| `/update-notes/<id>/` | POST | Save inline notes |
| `/bulk-action/` | POST | Bulk actions on selected papers |
| `/api/tags-autocomplete/` | GET | Tag autocomplete suggestions |
| `/api/poster/<id>/why-useful/` | GET | Generate / fetch a per-group "Why useful" snippet |
| `/api/set-my-primary-group/` | POST | Set the user's primary group |
| `/groups/`, `/groups/<id>/edit/`, `/groups/<id>/...` | various | Group management (group manager / admin) |
| `/groups/pending/<user_id>/dismiss/` | POST | Dismiss a no-group user from the pending banner |
| `/my-groups/` | GET | User's own groups + primary picker |
| `/users/` | GET | User admin (admin only) |
| `/users/<id>/toggle-superuser/` | POST | Promote / demote admin |
| `/users/<id>/toggle-group-manager/` | POST | Add / remove from `GestoreGruppi` |
| `/users/<id>/delete/` | POST | Hard-delete user (cascades to memberships, favorites, bot accounts, dismissal) |
| `/export/approved/csv/` | GET | Export approved papers (CSV) |
| `/export/approved/json/` | GET | Export approved papers (JSON) |
| `/telegram-webhook/` | POST | Telegram bot webhook |
| `/whatsapp-webhook/` | GET/POST | WhatsApp bot webhook |

---

## Monitoring

### View logs
```bash
docker compose logs -f django        # Web server
docker compose logs -f worker        # Celery tasks
docker compose logs -f beat          # Scheduled tasks
docker compose logs -f               # All services
```

### Check container health
```bash
docker compose ps
```

### Restart after code changes
```bash
docker compose up --build -d
```

---

## Notes

- Media files (poster images) are served directly by Apache via the `/app/media/` volume mount
- The AI pipeline handles non-research images, API errors, and network failures gracefully
- Failed analyses are kept in DB for retry -- only duplicates and non-scientific images are auto-deleted
- Bot media downloads run asynchronously in Celery to prevent webhook timeouts
- Redis is used both as the Celery broker and as a Django cache backend for bot state management
- Redis locks prevent duplicate processing of the same poster across workers
- Celery Beat schedule is persisted in a Docker volume to survive container restarts
- Error details are logged server-side only -- bot and web users receive generic error messages


## Regression tests

Run the group-management navigation and authorization checks with an isolated
in-memory SQLite database and cache:

```bash
python manage.py test bot_engine --settings=tesi_project.test_settings --noinput
```

From the production Compose directory, run them in a temporary container with
no network or production volumes:

```bash
docker run --rm --network none posterhub-django python manage.py test bot_engine --settings=tesi_project.test_settings --noinput
```
