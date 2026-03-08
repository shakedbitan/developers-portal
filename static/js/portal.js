/**
 * portal.js
 * - 2-row grid horizontal scroll
 * - Drag-to-scroll
 * - Search bar (sites + apps)
 * - Version picker modal
 * - Live app data refresh
 * - Theme toggle (dark / baby pink)
 */

// ── Scroll helpers ────────────────────────────────────────────────────────────
function scrollOuter(outerId, direction) {
  const outer = document.getElementById(outerId);
  if (!outer) return;
  const cardW = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--card-w')) || 200;
  outer.scrollBy({ left: direction * (cardW + 16) * 3, behavior: "smooth" });
}

// Drag-to-scroll on all grid rows
document.querySelectorAll(".cards-scroll-outer").forEach((grid) => {
  let isDown = false, startX, scrollLeft;
  grid.addEventListener("mousedown", (e) => {
    isDown = true;
    grid.style.cursor = "grabbing";
    startX = e.pageX - grid.offsetLeft;
    scrollLeft = grid.scrollLeft;
  });
  grid.addEventListener("mouseleave", () => { isDown = false; grid.style.cursor = ""; });
  grid.addEventListener("mouseup",    () => { isDown = false; grid.style.cursor = ""; });
  grid.addEventListener("mousemove",  (e) => {
    if (!isDown) return;
    e.preventDefault();
    grid.scrollLeft = scrollLeft - (e.pageX - grid.offsetLeft - startX) * 1.4;
  });
});

// ── Modal ─────────────────────────────────────────────────────────────────────
const overlay     = document.getElementById("modal-overlay");
const modalIcon   = document.getElementById("modal-icon");
const modalName   = document.getElementById("modal-app-name");
const versionList = document.getElementById("version-list");

let appsData  = typeof APPS  !== "undefined" ? APPS  : [];
let sitesData = typeof SITES !== "undefined" ? SITES : [];

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
          <span class="ver-label">${escHtml(ver.label)}</span>
          <span class="ver-filename">${escHtml(ver.filename)}</span>
        </div>
        <a class="dl-btn" href="/download/${encodeURIComponent(ver.smb_path)}"
           download="${escHtml(ver.filename)}">
          ↓ Download
        </a>`;
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

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeModal(); clearSearch(); }
});

// ── Search ────────────────────────────────────────────────────────────────────
const searchInput   = document.getElementById("search-input");
const searchClear   = document.getElementById("search-clear");
const searchResults = document.getElementById("search-results");

function handleSearch(query) {
  const q = query.trim().toLowerCase();

  // Toggle clear button
  searchClear.classList.toggle("visible", q.length > 0);

  if (!q) {
    searchResults.classList.remove("open");
    searchResults.innerHTML = "";
    return;
  }

  // Filter sites
  const matchedSites = sitesData.filter(s =>
    s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
  ).slice(0, 6);

  // Filter apps
  const matchedApps = appsData.filter(a =>
    a.name.toLowerCase().includes(q) || a.display_name.toLowerCase().includes(q)
  ).slice(0, 6);

  if (matchedSites.length === 0 && matchedApps.length === 0) {
    searchResults.innerHTML = `<div class="search-no-results">No results for "<strong>${escHtml(q)}</strong>"</div>`;
    searchResults.classList.add("open");
    return;
  }

  let html = "";

  if (matchedSites.length > 0) {
    html += `<div class="search-group-label">Web Apps</div>`;
    matchedSites.forEach(site => {
      html += `
        <a class="search-result-item" href="${escHtml(site.url)}" target="_blank" rel="noopener noreferrer"
           onclick="clearSearch()">
          <img class="search-result-img" src="${escHtml(site.image_url || '/static/icons/_placeholder.svg')}"
               alt="${escHtml(site.name)}" onerror="this.src='/static/icons/_placeholder.svg'"/>
          <div class="search-result-info">
            <span class="search-result-name">${highlight(site.name, q)}</span>
            <span class="search-result-sub">${escHtml(site.url)}</span>
          </div>
          <span class="search-result-badge badge-web">web</span>
        </a>`;
    });
  }

  if (matchedApps.length > 0) {
    html += `<div class="search-group-label">App Downloads</div>`;
    matchedApps.forEach(app => {
      const idx = appsData.indexOf(app);
      html += `
        <div class="search-result-item" onclick="searchOpenApp(${idx})">
          <img class="search-result-img" src="${escHtml(app.icon_url || '/static/icons/_placeholder.svg')}"
               alt="${escHtml(app.display_name)}" onerror="this.src='/static/icons/_placeholder.svg'"/>
          <div class="search-result-info">
            <span class="search-result-name">${highlight(app.display_name, q)}</span>
            <span class="search-result-sub">${app.versions ? app.versions.length : 0} version(s) available</span>
          </div>
          <span class="search-result-badge badge-app">install</span>
        </div>`;
    });
  }

  searchResults.innerHTML = html;
  searchResults.classList.add("open");
}

function searchOpenApp(index) {
  clearSearch();
  openVersionModal(index);
}

function clearSearch() {
  searchInput.value = "";
  searchClear.classList.remove("visible");
  searchResults.classList.remove("open");
  searchResults.innerHTML = "";
}

// Close search results when clicking outside
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-wrap")) {
    searchResults.classList.remove("open");
  }
});

// Re-open results when refocusing input with existing text
searchInput.addEventListener("focus", () => {
  if (searchInput.value.trim()) handleSearch(searchInput.value);
});

// ── Utilities ─────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function highlight(text, query) {
  const safe = escHtml(text);
  const safeQ = escHtml(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(safeQ, "gi"), m => `<mark style="background:var(--accent-glow);color:var(--accent);border-radius:2px">${m}</mark>`);
}

// ── Live refresh ──────────────────────────────────────────────────────────────
async function refreshAppsData() {
  try {
    const res = await fetch("/api/apps");
    if (!res.ok) return;
    appsData = await res.json();
  } catch (_) {}
}
setInterval(refreshAppsData, 60 * 1000);

// ── Theme toggle ──────────────────────────────────────────────────────────────
const THEME_KEY = "devportal-theme";

function applyTheme(theme) {
  const icon  = document.getElementById("theme-icon");
  const label = document.getElementById("theme-label");
  if (theme === "pink") {
    document.body.classList.add("theme-pink");
    icon.textContent  = "🌑";
    label.textContent = "Dark";
  } else {
    document.body.classList.remove("theme-pink");
    icon.textContent  = "🌸";
    label.textContent = "Pink";
  }
}

function toggleTheme() {
  const next = document.body.classList.contains("theme-pink") ? "dark" : "pink";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  applyTheme(next);
}

(function () {
  let saved = "dark";
  try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch (_) {}
  applyTheme(saved);
})();