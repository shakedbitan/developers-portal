/**
 * portal.js
 * Handles:
 *   - Horizontal scroll buttons
 *   - Version picker modal (open / close / populate)
 *   - Optional live refresh of app list via /api/apps
 */

// ── Scroll helpers ────────────────────────────────────────────────────────────
function scrollRow(rowId, direction) {
  const row = document.getElementById(rowId);
  if (!row) return;
  row.scrollBy({ left: direction * 320, behavior: "smooth" });
}

// Also allow mouse-drag scrolling on the rows
document.querySelectorAll(".cards-row").forEach((row) => {
  let isDown = false, startX, scrollLeft;
  row.addEventListener("mousedown", (e) => {
    isDown = true;
    row.style.cursor = "grabbing";
    startX = e.pageX - row.offsetLeft;
    scrollLeft = row.scrollLeft;
  });
  row.addEventListener("mouseleave", () => { isDown = false; row.style.cursor = ""; });
  row.addEventListener("mouseup",    () => { isDown = false; row.style.cursor = ""; });
  row.addEventListener("mousemove",  (e) => {
    if (!isDown) return;
    e.preventDefault();
    const x = e.pageX - row.offsetLeft;
    row.scrollLeft = scrollLeft - (x - startX) * 1.4;
  });
});

// ── Modal ─────────────────────────────────────────────────────────────────────
const overlay     = document.getElementById("modal-overlay");
const modalIcon   = document.getElementById("modal-icon");
const modalName   = document.getElementById("modal-app-name");
const versionList = document.getElementById("version-list");

// APPS is injected by the template as a JSON array
let appsData = typeof APPS !== "undefined" ? APPS : [];

function openVersionModal(index) {
  const app = appsData[index];
  if (!app) return;

  modalIcon.src = app.icon_url || "/static/icons/_placeholder.svg";
  modalName.textContent = app.display_name;

  versionList.innerHTML = "";

  if (!app.versions || app.versions.length === 0) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="ver-label" style="color:var(--text-muted)">No versions found</span>`;
    versionList.appendChild(li);
  } else {
    app.versions.forEach((ver) => {
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="ver-info">
          <span class="ver-label">${ver.label}</span>
          <span class="ver-filename">${ver.filename}</span>
        </div>
        <a class="dl-btn" href="/download/${encodeURIComponent(ver.smb_path)}"
           download="${ver.filename}">
          ↓ Download
        </a>
      `;
      versionList.appendChild(li);
    });
  }

  overlay.classList.add("open");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  overlay.classList.remove("open");
  document.body.style.overflow = "";
}

// Close on Escape
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ── Live refresh (optional) ───────────────────────────────────────────────────
// Polls /api/apps every 60s and silently updates the in-memory appsData.
// The page DOM is NOT rewritten on refresh — only the modal data updates.
// To see new apps in the UI the developer refreshes the page manually.
// This means a new version that appears on the share will show up in the
// version picker without a full page reload.

const REFRESH_INTERVAL_MS = 60 * 1000;

async function refreshAppsData() {
  try {
    const res = await fetch("/api/apps");
    if (!res.ok) return;
    const fresh = await res.json();
    appsData = fresh;
  } catch (_) {
    // silent — closed network may have intermittent issues
  }
}

setInterval(refreshAppsData, REFRESH_INTERVAL_MS);

// ── Theme toggle ──────────────────────────────────────────────────────────────
const THEME_KEY = "devportal-theme";

function applyTheme(theme) {
  const body   = document.body;
  const icon   = document.getElementById("theme-icon");
  const label  = document.getElementById("theme-label");

  if (theme === "pink") {
    body.classList.add("theme-pink");
    icon.textContent  = "🌑";
    label.textContent = "Dark";
  } else {
    body.classList.remove("theme-pink");
    icon.textContent  = "🌸";
    label.textContent = "Pink";
  }
}

function toggleTheme() {
  const current = document.body.classList.contains("theme-pink") ? "pink" : "dark";
  const next    = current === "pink" ? "dark" : "pink";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  applyTheme(next);
}

// Apply saved theme on load (no flash)
(function () {
  let saved = "dark";
  try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch (_) {}
  applyTheme(saved);
})();