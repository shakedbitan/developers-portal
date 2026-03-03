# Dev Portal

Internal developer portal with two sections:
- **Web Apps** — horizontal card strip linking to internal websites (favicon as icon)
- **App Downloads** — horizontal card strip pulling from a CIFS/SMB installs share (auto-extracted or manual icons, version picker modal, direct download)

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Backend | **Python / Flask** | Simple, you know it, easy to extend |
| SMB access | **smbprotocol** | Pure-Python SMB2/3 client — no OS mount needed in the pod |
| Icon extraction | **icoextract + Pillow** | Pulls `.ico` resources out of `.exe`/`.msi` files |
| Favicon service | **Google S2 Favicons** (proxied) | Zero-effort icons for web app cards |
| Templates | **Jinja2** (bundled with Flask) | Server-side HTML rendering |
| Frontend | Vanilla HTML + CSS + JS | No framework, no build step |
| Production server | **Gunicorn** | Multi-worker WSGI server |

---

## File Structure

```
devportal/
├── app.py               # Flask routes: /, /api/apps, /download/<path>, /favicon/<domain>
├── smb_scanner.py       # SMB directory walker + file streamer (with TTL cache)
├── icon_resolver.py     # Icon priority: manual PNG → extracted cache → placeholder
├── sites.json           # Web app links config
├── requirements.txt
├── Dockerfile
├── .env.example
│
├── static/
│   ├── css/style.css    # All styles (dark mode, horizontal rows, modal)
│   ├── js/portal.js     # Scroll buttons, drag-scroll, modal, 60s data refresh
│   └── icons/
│       ├── _placeholder.svg     # Fallback icon
│       ├── _cache/              # Auto-extracted icons (written at runtime)
│       ├── vscode.png           # ← manual icon overrides go here
│       └── git.png              #   filename = app folder name (lowercase)
│
└── templates/
    └── index.html       # Main page template
```

---

## Local Development

```bash
pip install -r requirements.txt
cp .env.example .env      # edit SMB values
python app.py
# → http://localhost:5000
```

For local dev **without** an SMB share, just leave `SMB_SERVER` empty — the App Downloads section will show a "not configured" message and the rest of the portal works normally.

---

## Adding Web App Links

Edit `sites.json`:

```json
[
  { "name": "Grafana", "url": "http://grafana.internal" },
  { "name": "My New Tool", "url": "http://newtool.internal" }
]
```

Favicon is fetched automatically from Google's service (proxied through the portal server so clients never leave the internal network). Rebuild & redeploy, or use a ConfigMap (see k8s section below).

---

## App Icons (Installs)

Priority order:

1. **Manual PNG** — drop a file named `static/icons/<appname>.png` into the repo.
   `appname` = the folder name on the SMB share, lowercase. E.g. `static/icons/vscode.png`
2. **Auto-extracted** — on first access, the portal downloads the first installer for the app,
   runs `icoextract` on it, converts to PNG and caches at `static/icons/_cache/<appname>.png`.
   This cache lives inside the container — it rebuilds on pod restart.
3. **Placeholder** — generic icon shown while extraction is pending or if extraction fails.

> **Tip:** Manual PNGs always win. If auto-extraction produces a bad result, just drop a clean
> 256×256 PNG in `static/icons/` and it will be used immediately.

---

## SMB Share Layout

```
\\SERVER\installs\
  vscode\
    VSCodeSetup-1.85.0.exe
    VSCodeSetup-1.86.0.exe
  git\
    Git-2.43-64-bit.exe
  nodejs\
    node-v20.11.0-x64.msi
  slack\
    SlackSetup-4.36.134.exe
```

- One **folder per application** — the folder name becomes the card title (title-cased)
- Drop any number of installer files inside — they all appear as versions
- Supported extensions: `.exe` `.msi` `.msix` `.appx` `.zip` `.7z`
- **Adding a new app**: create the folder, drop the file in. The portal picks it up within
  `SMB_CACHE_TTL` seconds (default 60s) — **no restart or redeploy needed**

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORTAL_TITLE` | `Dev Portal` | Page title |
| `TEAM_NAME` | `Engineering` | Team name in header |
| `SITES_FILE` | `sites.json` | Path to web links config |
| `PORT` | `5000` | Listening port |
| `DEBUG` | `false` | Flask debug mode |
| `SMB_SERVER` | _(empty)_ | FQDN or IP of the file server |
| `SMB_SHARE` | `installs` | Share name |
| `SMB_BASE_PATH` | _(empty)_ | Optional sub-folder inside the share |
| `SMB_USER` | _(empty)_ | Service account username |
| `SMB_PASSWORD` | _(empty)_ | Service account password |
| `SMB_DOMAIN` | _(empty)_ | Windows domain (optional) |
| `SMB_CACHE_TTL` | `60` | Seconds to cache the directory listing |

---

## Docker

```bash
docker build -t devportal .

docker run -p 5000:5000 \
  -e SMB_SERVER=fileserver.corp.local \
  -e SMB_SHARE=installs \
  -e SMB_USER=svc-devportal \
  -e SMB_PASSWORD=secret \
  -e SMB_DOMAIN=CORP \
  -e TEAM_NAME="Platform Engineering" \
  devportal
```

---

## Kubernetes

### Recommended: SMB credentials as a Secret, sites.json as a ConfigMap

```yaml
# secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: devportal-smb
type: Opaque
stringData:
  SMB_SERVER: "fileserver.corp.local"
  SMB_SHARE: "installs"
  SMB_USER: "svc-devportal"
  SMB_PASSWORD: "changeme"
  SMB_DOMAIN: "CORP"

---
# configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: devportal-sites
data:
  sites.json: |
    [
      {"name": "Grafana", "url": "http://grafana.internal"},
      {"name": "ArgoCD",  "url": "http://argocd.internal"}
    ]

---
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: devportal
spec:
  replicas: 1
  selector:
    matchLabels:
      app: devportal
  template:
    metadata:
      labels:
        app: devportal
    spec:
      containers:
        - name: devportal
          image: your-registry/devportal:latest
          ports:
            - containerPort: 5000
          envFrom:
            - secretRef:
                name: devportal-smb
          env:
            - name: PORTAL_TITLE
              value: "Dev Portal"
            - name: TEAM_NAME
              value: "Platform Engineering"
          volumeMounts:
            - name: sites-config
              mountPath: /app/sites.json
              subPath: sites.json
      volumes:
        - name: sites-config
          configMap:
            name: devportal-sites
```

> **Adding a web app link without redeploy:**
> `kubectl edit configmap devportal-sites` → save → portal picks it up on next page load.
>
> **Adding an install app:**
> Drop the folder+file on the SMB share. Portal auto-discovers within 60 seconds.
