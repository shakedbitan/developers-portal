# Eden — Developer Portal

Internal developer portal built by Team Genesys. Eden runs on Kubernetes and gives developers a single place to access internal web apps, download tools, and run team scripts via Argo Workflows.

---

## Features

**Web Apps** — Quick-access cards linking to internal websites. Two scrollable rows with favicon icons. Configured via eden-sites configmap (in the Tashtit)

**App Downloads** — Browse and download applications (.exe, .msi, .msix, .zip) directly from a CIFS/SMB network share. Cards are auto-discovered from the share directory structure. Version picker lets you choose between multiple versions of each app.

**Scripts** — Run team scripts (Python, Bash, PowerShell) via Argo Workflows directly from the UI. Each team gets its own scrollable row. Scripts are stored in a GitLab repository and loaded automatically. Fill in arguments through a form that enforces types, required fields, min/max rules and units. Upload new scripts through the UI — Eden opens a GitLab MR for team approval.

**Search** — Global search across web apps, install apps and scripts. Results appear instantly as you type.

**Light / Dark mode** — Toggle between dark (default) and light theme. Preference is saved in the browser.

---

## Architecture

```
Eden (k8s Pod — eden-namespace)
  │
  ├── reads web app links from   k8s ConfigMap (eden-sites)
  ├── reads config from          k8s ConfigMap (eden-config)
  ├── reads secrets from         k8s Secret   (eden-secrets)
  │
  ├── scans installers from      CIFS/SMB share  (smbprotocol — no OS mount needed)
  ├── fetches scripts from       GitLab API      (eden-scripts repo)
  └── submits workflows to       Argo Workflows API
```

Scripts live in the `eden-scripts` GitLab repo. After an MR is merged, GitLab CI calls Eden's reload webhook and the new script appears in the UI within seconds — no pod restart required.

---

## Repository Structure

```
eden/
├── app.py                  Flask application — all routes
├── config.py               Environment variable reading (single source of truth)
├── gitlab_client.py        GitLab API — fetch scripts, create branches, open MRs
├── argo_client.py          Argo Workflows API — submit workflows
├── script_store.py         In-memory script cache with background reload
├── smb_scanner.py          SMB share directory scanner and file streamer
├── icon_resolver.py        App icon extraction from .exe/.msi files
├── requirements.txt
├── Dockerfile
│
├── templates/
│   └── index.html          Main page (Jinja2 template)
│
├── static/
│   ├── css/style.css       All styles — dark/light themes, cards, modals, forms
│   ├── js/portal.js        All frontend JS — scroll, search, modals, validation
│   ├── icons/
│   │   ├── _placeholder.svg          Generic fallback icon
│   │   └── _logoplaceholder.png      ← You must add this (script card fallback)
│   └── site-images/                  Drop <sitename>.png here for web app cards
│
└── k8s/
    ├── configmap-eden.yaml       Main configuration
    ├── configmap-sites.yaml      Web app links (mounted as /config/sites.json)
    ├── secret-eden.yaml          All secrets template
    ├── deployment.yaml           Deployment + Service + Ingress
    └── gitlab-ci-webhook.yml     GitLab CI snippet for reload webhook
```

---

## Eden Scripts Repository Structure

Scripts live in a separate GitLab repo (`eden-scripts`). The structure must follow this convention:

```
eden-scripts/
└── scripts/
    └── <team-name>/
        └── <script-name>/        ← folder name must be kebab-case (e.g. rotate-secret)
            ├── script.py         ← or script.sh / script.ps1
            ├── script.yaml       ← metadata (see format below)
            └── logo.png          ← optional, shown as card image
```

### script.yaml format

```yaml
name: rotate-secret
namespace: db                  # Argo namespace = this + "-workflows" → db-workflows
language: python               # python | bash | powershell
description: Rotate a Kubernetes secret value and update dependent pods

approval:
  required: true               # if true, workflow pauses for approval in Argo UI

resources:
  cpu: 200m
  memory: 256Mi

dependencies:
  - kubernetes
  - hvac

args:
  - name: secret-name          # kebab-case
    type: string               # string | integer | boolean
    required: true
    description: Name of the secret to rotate

  - name: ttl
    type: integer
    required: false
    min: 1
    max: 365
    unit: days
    description: How long the new secret should be valid
```

Supported arg types:

| Type | UI control | Supports min/max |
|---|---|---|
| `string` | Text input | No |
| `integer` | Number input | Yes |
| `boolean` | Toggle switch | No |

---

## Kubernetes Setup

### 1. Apply ConfigMaps and Secrets

```bash
# Edit these files with your actual values first
kubectl apply -f k8s/configmap-eden.yaml
kubectl apply -f k8s/configmap-sites.yaml
kubectl apply -f k8s/secret-eden.yaml
```

### 2. Deploy Eden

```bash
kubectl apply -f k8s/deployment.yaml
```

### 3. Verify

```bash
kubectl get pods -n eden-namespace
kubectl logs -n eden-namespace -l app=eden -f
```

---

## Configuration Reference

All configuration is injected via the `eden-config` ConfigMap and `eden-secrets` Secret as environment variables.

### eden-config (ConfigMap)

| Variable | Example | Description |
|---|---|---|
| `PORTAL_TITLE` | `Eden` | Page title shown in the header |
| `TEAM_NAME` | `Platform Engineering` | Subtitle shown under the title |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `TEAMS` | `db,backend,infra` | Comma-separated team names. One script row per team. |
| `GITLAB_URL` | `https://git` | Your GitLab base URL |
| `GITLAB_REPO_PATH` | `my-group/eden-scripts` | Full path to the scripts repo |
| `GITLAB_DEFAULT_BRANCH` | `main` | Branch to read scripts from and open MRs against |
| `ARGO_URL` | `https://argoworkflow.example` | Argo Workflows base URL |
| `SMB_SERVER` | `fileserver.corp` | CIFS/SMB server hostname. Leave empty to disable installs. |
| `SMB_SHARE` | `installs` | Share name |
| `SMB_BASE_PATH` | _(empty)_ | Optional sub-folder inside the share |
| `SMB_DOMAIN` | `CORP` | Windows domain (optional) |
| `SMB_CACHE_TTL` | `60` | Seconds to cache the SMB directory listing |
| `SITES_FILE` | `/config/sites.json` | Path to the web app links file (ConfigMap mount) |
| `SCRIPTS_BASE_PATH` | `scripts` | Folder inside eden-scripts repo that contains team folders |

### eden-secrets (Secret)

| Variable | Description |
|---|---|
| `GITLAB_TOKEN` | GitLab personal access token (needs `api` + `read_repository` + `write_repository`) |
| `ARGO_TOKEN` | Argo Workflows bearer token |
| `SMB_USER` | SMB service account username |
| `SMB_PASSWORD` | SMB service account password |
| `RELOAD_TOKEN` | Shared secret for the `/api/scripts/reload` webhook. Generate with `openssl rand -hex 32` |
| `ARTIFACTORY_TOKEN` | Example — add any additional tokens your scripts need here |

---

## Web App Links

Web app links are stored in the `eden-sites` ConfigMap as a JSON file mounted at `/config/sites.json`. You can update links without rebuilding the image 


### Adding card images for web apps

Drop a PNG file named after the site into `static/site-images/`. The filename is the site name lowercased with spaces replaced by underscores:

```
static/site-images/
  grafana.png
  harbor_registry.png
  argocd.png
```

If no image is found, Eden fetches the site's `/favicon.ico` automatically (proxied through the pod — no browser traffic leaves the cluster). The favicon is cached locally so it only fetches once.

---

## App Downloads (SMB)

Eden connects directly to your CIFS/SMB share using `smbprotocol` — no OS-level mount is required in the pod. The share is scanned at startup and cached for `SMB_CACHE_TTL` seconds.

Expected share layout:

```
\\server\installs\
  vscode\
    VSCodeSetup-1.85.0.exe
    VSCodeSetup-1.86.0.exe
  git\
    Git-2.43-64-bit.exe
  nodejs\
    node-v20.11.0-x64.msi
```

One folder per application. Multiple installer files inside = multiple versions in the version picker. Supported extensions: `.exe` `.msi` `.msix` `.appx` `.zip` `.7z`

Adding a new app: create the folder and drop the file in. Eden picks it up within `SMB_CACHE_TTL` seconds — no restart needed.

### App icons

Priority order for install app card icons:

1. `static/icons/<appname>.png` — manual override in the repo (always wins)
2. Auto-extracted from the installer file using `icoextract` + `Pillow`
3. `static/icons/_placeholder.svg` — generic fallback

---

## Scripts

### How it works

1. Eden fetches all `script.yaml` files from the `eden-scripts` GitLab repo at startup
2. Scripts are grouped by team and displayed as scrollable card rows
3. Developer clicks a script card → form appears with the script's defined arguments
4. Developer fills in the form → Eden validates all inputs
5. Eden submits a workflow to Argo Workflows API in the team's namespace (`<team>-workflows`)
6. If `approval.required: true`, the workflow pauses at a suspend step — a team lead approves it in the Argo Workflows UI
7. The script runs in a pod cloned from the GitLab repo

### Adding a new script via the UI

Click **Upload New Script** at the bottom of the page. Fill in the form — Eden will:
- Validate all inputs (script name must be `kebab-case`, file extension must match language, etc.)
- Create a new branch in `eden-scripts`
- Push `script.yaml`, the script file, and optionally `logo.png`
- Open a GitLab MR for your team to review

After the MR is merged, GitLab CI calls Eden's reload webhook and the script appears in the UI automatically.

### Adding a new script manually

```bash
# 1. Create the folder structure
mkdir -p scripts/db/rotate-secret

# 2. Add your script file
cp my_script.py scripts/db/rotate-secret/script.py

# 3. Add script.yaml (see format above)

# 4. Optionally add a logo
cp logo.png scripts/db/rotate-secret/logo.png

# 5. Commit, push and open an MR
git add scripts/db/rotate-secret/
git commit -m "feat: add rotate-secret script"
git push origin feat/add-rotate-secret
```

### Secrets in scripts

Secrets from `eden-secrets` are mounted into workflow pods at `/etc/eden-secrets/` — one file per key. Scripts read them like this:

**Python:**
```python
def get_secret(name):
    try:
        with open(f"/etc/eden-secrets/{name}") as f:
            return f.read().strip()
    except FileNotFoundError:
        import os
        return os.getenv(name)  # fallback for local dev

token = get_secret("ARTIFACTORY_TOKEN")
```

**Bash:**
```bash
get_secret() {
  local path="/etc/eden-secrets/$1"
  [ -f "$path" ] && cat "$path" || echo "${!1}"
}
TOKEN=$(get_secret "ARTIFACTORY_TOKEN")
```

**PowerShell:**
```powershell
function Get-Secret($name) {
    $path = "/etc/eden-secrets/$name"
    if (Test-Path $path) { return (Get-Content $path -Raw).Trim() }
    return $env:$name
}
$token = Get-Secret "ARTIFACTORY_TOKEN"
```

The volume mount is defined in your ClusterWorkflowTemplate — Eden does not need to manage it.

### Reload webhook

When an MR is merged in `eden-scripts`, GitLab CI calls:

```
POST /api/scripts/reload
X-Reload-Token: <your RELOAD_TOKEN value>
```

Eden reloads all scripts in the background. Add the snippet from `k8s/gitlab-ci-webhook.yml` to your `eden-scripts` repo's `.gitlab-ci.yml` and set `EDEN_URL` and `EDEN_RELOAD_TOKEN` as masked CI/CD variables in the GitLab project settings.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Main portal page |
| `GET` | `/api/apps` | JSON list of SMB install apps and versions |
| `GET` | `/api/scripts` | JSON list of all scripts grouped by team |
| `POST` | `/api/scripts/reload` | Reload script cache from GitLab (requires `X-Reload-Token` header) |
| `POST` | `/api/scripts/submit` | Submit a workflow to Argo Workflows |
| `POST` | `/api/scripts/upload` | Upload a new script and open a GitLab MR |
| `GET` | `/api/scripts/<team>/<name>/logo` | Proxy script logo from GitLab |
| `GET` | `/download/<path>` | Stream an installer file from the SMB share |
| `GET` | `/favicon-proxy/<slug>` | Proxy a site's favicon from the internal network |

---

## Logging

All logs go to stdout in the format:

```
2024-01-15 10:23:45 INFO     app                  GET / — rendering main page
2024-01-15 10:23:45 DEBUG    gitlab_client        GitLab GET .../repository/tree → 200
2024-01-15 10:23:46 INFO     script_store         Team db: loaded 4 scripts
```

Set `LOG_LEVEL=DEBUG` in `eden-config` for full request and response logging. Token and password values are always redacted from logs regardless of log level.

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Python / Flask | API server and page rendering |
| Templates | Jinja2 | Server-side HTML rendering |
| Frontend | Vanilla HTML + CSS + JS | No build step, no framework |
| SMB | smbprotocol | Pure-Python CIFS client, no OS mount needed |
| Icon extraction | icoextract + Pillow | Pull icons from .exe/.msi files |
| HTTP client | requests | GitLab API, Argo API, favicon proxy |
| YAML parsing | PyYAML | Parse script.yaml files |
| Production server | Gunicorn | Multi-worker WSGI server |
| Container | Docker | Image build and deployment |
| Orchestration | Kubernetes | Runs in `eden-namespace` |
| Workflow engine | Argo Workflows | Script execution with approval gates |
| Script storage | GitLab | Source of truth for all scripts |

---

*Eden — Created by Team Genesys*