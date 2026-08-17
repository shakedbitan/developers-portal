# Eden — Developer Portal

Internal developer portal built by Shaked Bitan. Eden runs on Kubernetes and gives developers a single place to access internal web apps, browse a software download catalog, and run team scripts via Argo Workflows.

Flask API backend + React/Vite single-page frontend, backed by Postgres. See `CLAUDE.md` for full architecture notes (repo layout, DB schema, env vars, known issues).

---

## Features

**Home** — Starred web apps as a phone-home-screen-style grid. Apps sharing a `group_name` stack into one card with a color-coded environment picker (dev/staging/prod/...). Drag to reorder.

**Web Apps** — Browse and star all approved internal sites. Submit new ones (goes to an admin approval queue) or edit/delete as admin.

**Scripts** — Run team scripts (Python, Bash, PowerShell) via Argo Workflows directly from the UI. Scripts live in a GitLab repository and load automatically. The run form enforces each argument's type — text, number, boolean, dropdown (including dependent dropdowns), and `js_file` (upload a `.js` file, converted to base64 and passed straight through as the argument's value — nothing is stored server-side). Upload new scripts through the UI — Eden opens a GitLab MR for team approval.

**Downloads** — Search and browse an internal software catalog backed by S3-compatible object storage (StorageGRID). Each catalog entry can have multiple variants (version / architecture / OS / locale / product component); admins upload new artifacts directly through the UI.

**Search** — Global search across web apps, scripts, and the download catalog. Docks into the top bar outside of the Home screen.

**Light / Dark mode** — Toggle between dark (default) and light theme. Preference is saved in the browser.

---

## Repository structure

```
developers-portal/
├── backend/                    ← Flask API (all Python source)
│   ├── app.py                  ← app entrypoint, most routes
│   ├── auth.py                 ← oauth2-proxy integration
│   ├── config.py                ← env vars, BANNER_OPTIONS / ENV_COLOR_OPTIONS
│   ├── db.py                   ← Postgres helpers + schema migrations
│   ├── download_catalog.py     ← S3-backed download catalog (Blueprint)
│   ├── gitlab_client.py        ← GitLab API client
│   ├── argo_client.py          ← Argo Workflows client
│   ├── script_store.py         ← GitLab-backed scripts cache
│   ├── icon_resolver.py        ← favicon/icon resolution
│   └── smb_scanner.py          ← legacy SMB install-share scanner (kept for compat)
├── frontend/                   ← React + Vite SPA
│   └── src/
│       ├── api/index.js        ← all backend API calls
│       ├── components/         ← TopBar, SearchBar, SiteCard, GroupedSiteCard, ScriptCard,
│       │                          DownloadCard, DownloadUploadModal, Modal, Button, ...
│       └── pages/               ← Home, WebApps, Scripts, Downloads
├── k8s/                        ← Kubernetes manifests (ConfigMap, etc.)
├── tools/                      ← operational scripts (e.g. download-catalog reconciliation)
├── sites.json                  ← dev-only hardcoded site list (used when Postgres has no rows)
├── scripts.json                ← dev-only hardcoded scripts (used when GitLab returns none)
└── requirements.txt
```

`sites.json` and `scripts.json` are **local-development fallbacks only** — they let the app run and be worked on without a reachable Postgres/GitLab. A real deployment with a seeded database and GitLab access never touches them.

---

## Eden Scripts Repository Structure

```
eden-scripts/
└── <team-name>/
    └── <script-name>/        ← folder name must be kebab-case (e.g. rotate-secret)
        ├── script.py         ← or script.sh / script.ps1
        ├── script.yaml       ← metadata (see format below)
        └── logo.png          ← optional (.png, .jpg, .jpeg supported)
```

### script.yaml format

```yaml
name: rotate-secret
namespace: db                  # Argo namespace = this + "-workflows" → db-workflows
language: python               # python | bash | powershell
description: Rotate a Kubernetes secret value

approval:
  required: true               # pauses workflow for approval in Argo UI

resources:
  cpu: 200m
  memory: 256Mi

dependencies:
  - kubernetes
  - hvac

args:
  - name: secret-name
    type: string               # string | integer | boolean | select | js_file
    required: true
    description: Name of the secret to rotate
    example: my-db-password

  - name: ttl
    type: integer
    required: false
    min: 1
    max: 365
    unit: days
    description: How long the new secret should be valid
    example: 90

  - name: environment
    type: select
    required: true
    options:
      - dev
      - staging
      - prod
    example: dev

  - name: project
    type: select
    required: true
    depends_on: environment     # renders as dependent dropdown
    options:
      dev:
        - dev-project-a
        - dev-project-b
      staging:
        - staging-project-x
      prod:
        - prod-project-alpha
        - prod-project-beta

  - name: patch-script
    type: js_file               # renders a file picker, accepts only .js
    required: false
    description: Optional JS patch to run before the rotation
```

### Arg types

| Type | UI control | Extra fields |
|---|---|---|
| `string` | Text input | `example` |
| `integer` | Number input | `min`, `max`, `unit`, `example` |
| `boolean` | Toggle switch | `example` |
| `select` | Dropdown | `options` (list), `example` |
| `select` + `depends_on` | Dependent dropdown | `options` (dict keyed by parent value) |
| `js_file` | File picker (`.js` only) | — value the script receives is the file's content, base64-encoded, one line |

---

## Scripts

### How it works

1. Eden fetches all `script.yaml` files from GitLab at startup
2. Scripts are grouped by team — one section per team on the Scripts page
3. Developer clicks a card → form with the script's args appears
4. Developer fills the form (uploading a file for any `js_file` arg) → Eden validates inputs
5. Eden submits a workflow to Argo in the `<namespace>-workflows` namespace
6. If `approval.required: true` → workflow pauses at a suspend step for approval in the Argo UI
7. Script runs in a pod that clones the repo and executes the script
8. Script gets secrets as env vars (if needed) from the team's namespace secrets object

### Script args in the script itself

Args are passed as CLI arguments: `--arg-name value`. Use standard argument parsing:

**Python:**
```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--secret-name", required=True)
parser.add_argument("--ttl", type=int, default=90)
args = parser.parse_args()
```

**Bash:**
```bash
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --secret-name) SECRET_NAME="$2"; shift ;;
    --ttl) TTL="$2"; shift ;;
  esac
  shift
done
```

**PowerShell:**
```powershell
param(
  [string]$SecretName,
  [int]$Ttl = 90
)
```

A `js_file` arg arrives the same way as any other — `--patch-script <base64>` — decode it to get the original file's bytes.

### Reload webhook

After MR merge, GitLab CI calls:

```
POST /api/scripts/reload
X-Reload-Token: <RELOAD_TOKEN value>
```

```yaml
notify-eden-reload:
  stage: deploy
  rules:
    - if: $CI_COMMIT_BRANCH == "master"
  script:
    - |
      curl -s -X POST "${EDEN_URL}/api/scripts/reload" \
        -H "X-Reload-Token: ${EDEN_RELOAD_TOKEN}"
```

---

## Downloads (S3 catalog)

StorageGRID (S3-compatible) is the object storage backend. Each catalog item can have multiple variants — different versions, architectures, operating systems, locales, or product components (e.g. "Ultimate" vs "Team Foundation Server" under the same app). Uploads and presigned download URLs go through `backend/download_catalog.py`; `tools/reconcile_download_catalog.py` is an offline admin CLI for reconciling the catalog against an organized metadata file (dry-run by default).

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | React SPA (all client routes fall through to this) |
| `GET` | `/api/me` | Current username + admin flag |
| `GET` | `/api/sites` | Approved site list |
| `POST` | `/api/sites/submit` | Submit a new site for approval |
| `GET` | `/api/sites/pending` | Pending site submissions (admin) |
| `POST` | `/api/sites/review` | Approve/reject a submission (admin) |
| `POST` | `/api/sites/delete` | Delete a site (admin) |
| `POST` | `/api/sites/edit` | Edit a site (admin) |
| `GET` | `/api/stars` | Starred sites for the current user |
| `POST` | `/api/stars/add` \| `/remove` \| `/reorder` | Manage starred sites |
| `GET` | `/api/banner-options` \| `/api/env-color-options` | Available card banner / env-row colors |
| `GET` | `/api/apps` | SMB install apps (legacy, backend-only — no current UI consumer) |
| `GET` | `/api/scripts` | All scripts by team |
| `POST` | `/api/scripts/reload` | Reload script cache from GitLab (requires `X-Reload-Token`) |
| `POST` | `/api/scripts/submit` | Submit a workflow run to Argo |
| `POST` | `/api/scripts/upload` | Upload a new script → opens a GitLab MR |
| `POST` | `/api/scripts/upload-arg-file` | Convert a `.js` file to its base64 `js_file` arg value (stateless) |
| `GET` | `/api/scripts/<team>/<name>/logo` | Proxy a script's logo from GitLab |
| `GET` | `/api/scripts/pending` \| `POST /approve` \| `POST /reject` | Review script MRs (admin) |
| `GET` | `/api/admin/status` | Whether the current user is an admin |
| `GET` | `/api/admin/users` \| `POST /set` | Manage admin users (admin) |
| `GET` | `/api/downloads` | Search/browse the download catalog |
| `GET` | `/api/downloads/categories` | Category list with item counts |
| `GET` | `/api/downloads/<id>` | Single catalog item + its variants |
| `POST` | `/api/downloads/upload` | Upload a new artifact (admin) |
| `POST` | `/api/downloads/variants/<id>/url` | Generate a presigned S3 download URL |
| `GET` | `/download/<path>` | Stream an installer from the legacy SMB share |

---

## Logging

All logs go to stdout:

```
2026-04-01 10:23:45 INFO     app                  GET /
2026-04-01 10:23:45 DEBUG    gitlab_client        GitLab GET .../repository/tree → 200
2026-04-01 10:23:46 INFO     script_store         Team db: loaded 5 scripts
```

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Python / Flask | API server |
| Frontend | React + Vite | SPA — Home, Web Apps, Scripts, Downloads |
| Database | PostgreSQL | Sites, stars, users, submissions, download catalog |
| Object storage | S3-compatible (StorageGRID) + boto3 | Download catalog artifacts |
| Drag & drop | @dnd-kit | Home screen card reordering |
| SMB | smbprotocol | Legacy install-share scanner |
| Icon extraction | icoextract + Pillow | Pull icons from `.exe`/`.msi` |
| HTTP client | requests | GitLab API, Argo API |
| YAML parsing | PyYAML | Parse `script.yaml` files |
| Production server | Gunicorn | Multi-worker WSGI |
| Orchestration | Kubernetes | Runs in `eden-namespace` |
| Workflow engine | Argo Workflows | Script execution with approval gates |
| Script storage | GitLab | Source of truth for all scripts |
| Auth | oauth2-proxy (ADFS OIDC) | Sits in front of the app; `AUTH_ENABLED=false` for local dev |

---

## Local development

```bash
# Backend (from backend/)
pip install -r ../requirements.txt
python app.py            # serves on :5000, AUTH_ENABLED=false by default locally

# Frontend (from frontend/)
npm install
npm run dev               # Vite dev server on :5173, proxies /api to :5000
```

Without a reachable Postgres/GitLab, the app falls back to `sites.json` / `scripts.json` at the repo root so the UI is still usable end-to-end.

---

*Eden — Created by Shaked Bitan*
