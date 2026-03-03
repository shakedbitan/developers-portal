FROM python:3.12-slim

# Install cifs-utils for optional OS-level SMB mount support
RUN apt-get update && apt-get install -y --no-install-recommends \
    cifs-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create icon cache directory
RUN mkdir -p static/icons/_cache

ENV PORTAL_TITLE="Dev Portal"
ENV TEAM_NAME="Engineering"
ENV SITES_FILE="sites.json"
ENV PORT=5000

# SMB config — set via k8s Secret / env
# ENV SMB_SERVER=""
# ENV SMB_SHARE="installs"
# ENV SMB_BASE_PATH=""
# ENV SMB_USER=""
# ENV SMB_PASSWORD=""
# ENV SMB_DOMAIN=""
# ENV SMB_CACHE_TTL="60"

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
