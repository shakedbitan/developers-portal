# Eden — Developer Portal

Internal developer portal built by Shaked bitan. Eden runs on Kubernetes and gives developers a single place to access internal web apps, download tools, and run team scripts via Argo Workflows.

---

## Features

**Web Apps** — Quick-access cards linking to internal websites. Two scrollable rows with favicon icons and color-coded tags. Configured via a Kubernetes ConfigMap — no rebuild needed to add or remove links.

**App Downloads** — Browse and download Windows installers directly from a CIFS/SMB network share. Cards are auto-discovered from the share directory structure. Version picker lets you choose between multiple versions of each app.

**Scripts** — Run team scripts (Python, Bash, PowerShell) via Argo Workflows directly from the UI. Each team gets its own scrollable row. Scripts are stored in a GitLab repository and loaded automatically. Fill in arguments through a form that enforces types, required fields, min/max rules, units, and dependent selects. Upload new scripts through the UI — Eden opens a GitLab MR for team approval.

**Search** — Global search across web apps, install apps and scripts. Results appear instantly as you type.

**Light / Dark mode** — Toggle between dark (default) and light theme. Preference is saved in the browser.

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
    type: string               # string | integer | boolean | select
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
```

### Arg types

| Type | UI control | Extra fields |
|---|---|---|
| `string` | Text input | `example` |
| `integer` | Number input | `min`, `max`, `unit`, `example` |
| `boolean` | Toggle switch | `example` |
| `select` | Dropdown | `options` (list), `example` |
| `select` + `depends_on` | Dependent dropdown | `options` (dict keyed by parent value) |

---


## App Downloads (SMB)

Expected share layout:

```
\\server\installs\
  vscode\
    VSCodeSetup-1.85.0.exe
    VSCodeSetup-1.86.0.exe
  git\
    Git-2.43-64-bit.exe
```

One folder per app, multiple files = multiple versions. Supported: `.exe` `.msi` `.msix` `.appx` `.zip` `.7z`

New apps appear automatically within `SMB_CACHE_TTL` seconds — no restart needed.

---

## Scripts

### How it works

1. Eden fetches all `script.yaml` files from GitLab at startup
2. Scripts are grouped by team — one scrollable row per team
3. Developer clicks a card → form with the script's args appears
4. Developer fills form → Eden validates inputs
5. Eden submits workflow to Argo in `<namespace>-workflows` namespace
6. If `approval.required: true` → workflow pauses at suspend step for approval in Argo UI
7. Script runs in a pod that clones the repo and executes the script
8. script gets secrets as env vars (if needed) from the team's namespace secrets object



### Script args in the script itself (from portal's UI)

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

### Reload webhook

After MR merge, GitLab CI calls:

```
POST /api/scripts/reload
X-Reload-Token: <RELOAD_TOKEN value>
```

when approved (new script is added to the script repository) this gitlab-ci.yaml runs and the portal reload the scripts list from the scripts repository.

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

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Main portal page |
| `GET` | `/api/apps` | JSON list of SMB install apps |
| `GET` | `/api/scripts` | JSON list of all scripts by team |
| `POST` | `/api/scripts/reload` | Reload script cache (requires `X-Reload-Token`) |
| `POST` | `/api/scripts/submit` | Submit workflow to Argo |
| `POST` | `/api/scripts/upload` | Upload new script → GitLab MR |
| `GET` | `/api/scripts/<team>/<name>/logo` | Proxy script logo from GitLab |
| `GET` | `/download/<path>` | Stream installer from SMB |
| `GET` | `/favicon-proxy/<slug>` | Proxy site favicon |

---

## Logging

All logs go to stdout:

```
2026-04-01 10:23:45 INFO     app                  GET /
2026-04-01 10:23:45 DEBUG    gitlab_client        GitLab GET .../repository/tree → 200
2026-04-01 10:23:46 INFO     script_store         Team Cyber: loaded 5 scripts
```
---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Python / Flask | API server and page rendering |
| Templates | Jinja2 | Server-side HTML |
| Frontend | Vanilla HTML + CSS + JS | No build step, no framework |
| SMB | smbprotocol | Pure-Python CIFS — no OS mount needed |
| Icon extraction | icoextract + Pillow | Pull icons from .exe/.msi |
| HTTP client | requests | GitLab API, Argo API, favicon proxy |
| YAML parsing | PyYAML | Parse script.yaml files |
| Production server | Gunicorn | Multi-worker WSGI |
| Orchestration | Kubernetes | Runs in `eden-namespace` |
| Workflow engine | Argo Workflows | Script execution with approval gates |
| Script storage | GitLab | Source of truth for all scripts |

---

*Eden — Created by Team Shaked Bitan*