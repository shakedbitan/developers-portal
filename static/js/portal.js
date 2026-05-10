/**
 * portal.js — Eden Developer Portal
 *
 * Handles:
 *   - Horizontal scroll (outer wrapper pattern)
 *   - Drag-to-scroll
 *   - Search (sites + apps + scripts)
 *   - Version picker modal (installs)
 *   - Script run modal (arg form + validation + Argo submit)
 *   - Script upload modal (full form + validation + GitLab MR)
 *   - Theme toggle (dark / light)
 */

// ── Data injected by Jinja ────────────────────────────────────────────────────
let appsData         = typeof APPS           !== "undefined" ? APPS           : [];
let sitesData        = typeof SITES          !== "undefined" ? SITES          : [];
const teamsData      = typeof TEAMS          !== "undefined" ? TEAMS          : [];
let scriptsByTeam    = typeof SCRIPTS_BY_TEAM !== "undefined" ? SCRIPTS_BY_TEAM : {};

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(str) {
  return String(str ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ── Tag colors ───────────────────────────────────────────────────────────────
// Pastel palette — each tag name always maps to the same color
const TAG_PALETTE = [
  { bg: "#ffd6e0", text: "#7d2340" },  // pastel rose
  { bg: "#fce4b3", text: "#7a4a00" },  // pastel amber
  { bg: "#d4f0c4", text: "#2d6a1f" },  // pastel green
  { bg: "#c9e8ff", text: "#1a4f7a" },  // pastel blue
  { bg: "#e8d5ff", text: "#5a1f8a" },  // pastel purple
  { bg: "#ffd9c4", text: "#7a3010" },  // pastel orange
  { bg: "#c4f0ee", text: "#1a5f5c" },  // pastel teal
  { bg: "#ffd6f5", text: "#7a1a6e" },  // pastel pink
  { bg: "#e0f0c4", text: "#3d6020" },  // pastel lime
  { bg: "#d6e4ff", text: "#1a3070" },  // pastel indigo
  { bg: "#ffe8c4", text: "#7a4a00" },  // pastel peach
  { bg: "#c4e8d6", text: "#1a5f3d" },  // pastel mint
];

// Simple deterministic hash: same tag name always → same palette index
function _tagHash(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = (h * 31 + str.charCodeAt(i)) >>> 0;
  }
  return h % TAG_PALETTE.length;
}

// Apply colors to all .site-tag elements on the page
function applyTagColors() {
  document.querySelectorAll(".site-tag").forEach(el => {
    const tag    = el.getAttribute("data-tag") || el.textContent.trim();
    const palette = TAG_PALETTE[_tagHash(tag)];
    el.style.background = palette.bg;
    el.style.color      = palette.text;
    el.style.border     = `1px solid ${palette.text}30`;
  });
}

// Run once on load — wrapped safely so any tag error never breaks other JS
document.addEventListener("DOMContentLoaded", () => {
  try { applyTagColors(); } catch(e) { console.warn("applyTagColors error:", e); }
});

// ── Scroll ────────────────────────────────────────────────────────────────────
function scrollOuter(outerId, direction) {
  const outer = document.getElementById(outerId);
  if (!outer) return;
  const cardW = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--card-w")) || 200;
  outer.scrollBy({ left: direction * (cardW + 16) * 3, behavior: "smooth" });
}

// Drag-to-scroll on all scroll outers
document.querySelectorAll(".cards-scroll-outer").forEach(el => {
  let down = false, startX, scrollLeft;
  el.addEventListener("mousedown",  e => { down = true; el.style.cursor = "grabbing"; startX = e.pageX - el.offsetLeft; scrollLeft = el.scrollLeft; });
  el.addEventListener("mouseleave", () => { down = false; el.style.cursor = ""; });
  el.addEventListener("mouseup",    () => { down = false; el.style.cursor = ""; });
  el.addEventListener("mousemove",  e => { if (!down) return; e.preventDefault(); el.scrollLeft = scrollLeft - (e.pageX - el.offsetLeft - startX) * 1.4; });
});

// ── Version picker modal (installs) ──────────────────────────────────────────
const installOverlay = document.getElementById("modal-overlay");

function openVersionModal(index) {
  const app = appsData[index];
  if (!app) return;
  document.getElementById("modal-icon").src = app.icon_url || "/static/icons/_placeholder.svg";
  document.getElementById("modal-app-name").textContent = app.display_name;
  const list = document.getElementById("version-list");
  list.innerHTML = "";
  if (!app.versions || app.versions.length === 0) {
    list.innerHTML = `<li><span class="ver-label" style="color:var(--text-muted)">No versions found</span></li>`;
  } else {
    app.versions.forEach(ver => {
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="ver-info">
          <span class="ver-label">${esc(ver.label)}</span>
          <span class="ver-filename">${esc(ver.filename)}</span>
        </div>
        <a class="dl-btn" href="/download/${encodeURIComponent(ver.smb_path)}" download="${esc(ver.filename)}">↓ Download</a>`;
      list.appendChild(li);
    });
  }
  openOverlay(installOverlay);
}

function closeModal() { closeOverlay(installOverlay); }

// ── Script run modal ──────────────────────────────────────────────────────────
const scriptOverlay = document.getElementById("script-modal-overlay");
let _currentScript  = null;

function openScriptModal(team, scriptName) {
  const scripts = scriptsByTeam[team] || [];
  const script  = scripts.find(s => s.folder_name === scriptName);
  if (!script) { console.warn("Script not found:", team, scriptName); return; }
  _currentScript = script;

  document.getElementById("script-modal-icon").src =
    `/api/scripts/${esc(team)}/${esc(scriptName)}/logo`;
  document.getElementById("script-modal-name").textContent = script.name;
  document.getElementById("script-modal-desc").textContent = script.description;

  const body = document.getElementById("script-modal-body");
  body.innerHTML = "";

  if (!script.args || script.args.length === 0) {
    body.innerHTML = `<p style="font-family:var(--font-mono);font-size:0.8rem;color:var(--text-muted);padding:8px 0">
      This script has no parameters. Click Run to submit.
    </p>`;
  } else {
    script.args.forEach(arg => {
      const required = arg.required ? `<span class="run-required-tag">*required</span>` : "";
      const unit     = arg.unit ? `<span class="run-unit">${esc(arg.unit)}</span>` : "";
      const hint     = buildArgHint(arg);

      let input = "";
      if (arg.type === "boolean") {
        const checked = arg.default === true || arg.default === "true" ? "checked" : "";
        const exHint  = arg.example ? `<div class="field-hint">e.g. ${esc(arg.example)}</div>` : "";
        input = `
          <div class="toggle-wrap" style="margin-top:6px">
            <label class="toggle-switch">
              <input type="checkbox" id="arg-${esc(arg.name)}" ${checked}/>
              <span class="toggle-slider"></span>
            </label>
            <span class="toggle-label">Yes / No</span>
          </div>${exHint}`;
      } else if (arg.type === "select") {
        const depOn      = arg.depends_on || "";
        const isDependent = !!(depOn && !Array.isArray(arg.options) && typeof arg.options === "object");

        let opts = "";
        if (!isDependent) {
          // Simple select — render all options immediately
          opts = (arg.options || []).map(o =>
            `<option value="${esc(String(o))}">${esc(String(o))}</option>`
          ).join("");
        }

        // onchange handler — always reacts to self + notifies children
        const onchangeHandler = `onSelectChange('${esc(arg.name)}')`;

        input = `
          <select class="run-input" id="arg-${esc(arg.name)}"
                  onchange="${onchangeHandler}"
                  ${isDependent ? 'disabled data-dependent="true"' : ''}>
            <option value="">-- select ${esc(arg.name)} --</option>
            ${opts}
          </select>
          ${isDependent ? `<div class="field-hint">Depends on: <strong>${esc(depOn)}</strong></div>` : ""}`;
      } else {
        const inputType  = arg.type === "integer" ? "number" : "text";
        const minAttr    = arg.min !== null && arg.min !== undefined ? `min="${arg.min}"` : "";
        const maxAttr    = arg.max !== null && arg.max !== undefined ? `max="${arg.max}"` : "";
        const placeholder = arg.example ? esc(String(arg.example)) : (arg.required ? "Required" : "Optional");
        const defVal     = arg.default !== undefined && arg.default !== "" ? `value="${esc(String(arg.default))}"` : "";
        input = `
          <div class="run-field-wrap">
            <input class="run-input" id="arg-${esc(arg.name)}"
                   type="${inputType}" ${minAttr} ${maxAttr} ${defVal}
                   placeholder="${placeholder}"
                   oninput="validateRunField('${esc(arg.name)}')"/>
            ${unit}
          </div>`;
      }

      const div = document.createElement("div");
      div.className = "run-form-group";
      div.innerHTML = `
        <label class="form-label">${esc(arg.name)}${required}
          ${arg.description ? `<span style="font-weight:400;text-transform:none;letter-spacing:0"> — ${esc(arg.description)}</span>` : ""}
        </label>
        ${input}
        ${hint ? `<div class="field-hint">${hint}</div>` : ""}
        <div class="run-field-error" id="arg-${esc(arg.name)}-err"></div>`;
      body.appendChild(div);
    });
  }

  clearElement("script-modal-error");
  openOverlay(scriptOverlay);
}

// Called when a parent select changes — re-renders any child selects that depend on it
// Single handler for all select changes — validates self and re-renders children
function onSelectChange(argName) {
  if (!_currentScript) return;
  const el  = document.getElementById(`arg-${argName}`);
  if (!el) return;

  // Validate this field
  validateRunField(argName);

  // Re-render any child selects that depend on this arg
  (_currentScript.args || []).forEach(childArg => {
    if ((childArg.depends_on || "") !== argName) return;

    const childEl = document.getElementById(`arg-${childArg.name}`);
    if (!childEl) return;

    const parentVal  = el.value;
    const optionsMap = childArg.options || {};

    // Clear child
    childEl.innerHTML = "";

    if (!parentVal) {
      // Parent has no value — disable child
      childEl.disabled = true;
      const placeholder = document.createElement("option");
      placeholder.value    = "";
      placeholder.textContent = `Select ${argName} first`;
      childEl.appendChild(placeholder);
    } else {
      // Parent has value — populate child with matching options
      const choices = Array.isArray(optionsMap)
        ? optionsMap
        : (optionsMap[parentVal] || []);

      childEl.disabled = false;

      // Always start with a blank prompt
      const blank = document.createElement("option");
      blank.value       = "";
      blank.textContent = `-- select ${childArg.name} --`;
      childEl.appendChild(blank);

      choices.forEach(o => {
        const opt = document.createElement("option");
        opt.value       = String(o);
        opt.textContent = String(o);
        childEl.appendChild(opt);
      });
    }

    // Clear child error
    const errEl = document.getElementById(`arg-${childArg.name}-err`);
    if (errEl) errEl.textContent = "";
    childEl.classList.remove("error");
  });
}

function buildArgHint(arg) {
  const parts = [];
  if (arg.type === "integer") {
    if (arg.min !== null && arg.min !== undefined) parts.push(`min: ${arg.min}${arg.unit ? " " + arg.unit : ""}`);
    if (arg.max !== null && arg.max !== undefined) parts.push(`max: ${arg.max}${arg.unit ? " " + arg.unit : ""}`);
  }
  if (arg.type) parts.unshift(`type: ${arg.type}`);
  return parts.join(" · ");
}

function validateRunField(argName) {
  if (!_currentScript) return true;
  const argDef = (_currentScript.args || []).find(a => a.name === argName);
  if (!argDef) return true;

  const input = document.getElementById(`arg-${argName}`);
  const errEl = document.getElementById(`arg-${argName}-err`);
  if (!input || !errEl) return true;

  const val = input.value.trim();
  let error = "";

  if (argDef.required && !val) {
    error = "This field is required";
  } else if (argDef.type === "select") {
    const optionsMap = argDef.options || [];
    if (argDef.depends_on && !Array.isArray(optionsMap)) {
      // Dependent select — only validate if parent has a value AND child has a value
      const parentEl  = document.getElementById(`arg-${argDef.depends_on}`);
      const parentVal = parentEl ? parentEl.value : "";
      if (!parentVal || !val) {
        // Parent not chosen or child blank — clear error, skip
        input.classList.remove("error");
        errEl.textContent = "";
        return !argDef.required || !!val;
      }
      const validOpts = (optionsMap[parentVal] || []).map(String);
      if (validOpts.length && !validOpts.includes(val)) {
        error = `Must be one of: ${validOpts.join(", ")}`;
      }
    } else if (val) {
      // Simple select — validate against flat options list
      const validOpts = Array.isArray(optionsMap) ? optionsMap.map(String) : [];
      if (validOpts.length && !validOpts.includes(val)) {
        error = `Must be one of: ${validOpts.join(", ")}`;
      }
    }
  } else if (val && argDef.type === "integer") {
    const n = Number(val);
    if (!Number.isInteger(n)) {
      error = "Must be a whole number";
    } else if (argDef.min !== null && argDef.min !== undefined && n < argDef.min) {
      error = `Must be at least ${argDef.min}${argDef.unit ? " " + argDef.unit : ""}`;
    } else if (argDef.max !== null && argDef.max !== undefined && n > argDef.max) {
      error = `Must be at most ${argDef.max}${argDef.unit ? " " + argDef.unit : ""}`;
    }
  }

  input.classList.toggle("error", !!error);
  errEl.textContent = error;
  return !error;
}

async function submitScript() {
  if (!_currentScript) return;
  const btn = document.getElementById("script-submit-btn");

  // Validate all args
  let valid = true;
  const userArgs = {};
  for (const arg of (_currentScript.args || [])) {
    const el = document.getElementById(`arg-${arg.name}`);
    if (!el) continue;
    if (arg.type === "boolean") {
      userArgs[arg.name] = el.checked ? "true" : "false";
    } else {
      userArgs[arg.name] = el.value.trim();
    }
    if (!validateRunField(arg.name)) valid = false;
    // Extra check: required dependent select must have a value
    if (arg.type === "select" && arg.required && !el.value.trim()) {
      const errEl = document.getElementById(`arg-${arg.name}-err`);
      if (errEl) errEl.textContent = "This field is required";
      el.classList.add("error");
      valid = false;
    }
  }

  if (!valid) {
    showModalError("script-modal-error", "Please fix the errors above before submitting.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Submitting…";
  clearElement("script-modal-error");

  const payload = {
    team:             _currentScript.team,
    script_name:      _currentScript.folder_name,
    language:         _currentScript.language,
    script_path:      _currentScript.script_file,
    args:             userArgs,
    dependencies:     _currentScript.dependencies || [],
    approval_required: _currentScript.approval_required,
    resources:        _currentScript.resources || {},
  };

  try {
    const resp = await fetch("/api/scripts/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();

    if (!resp.ok) {
      // Show field-level errors if returned
      if (data.field_errors) {
        Object.entries(data.field_errors).forEach(([name, msg]) => {
          const el = document.getElementById(`arg-${name}`);
          const errEl = document.getElementById(`arg-${name}-err`);
          if (el) el.classList.add("error");
          if (errEl) errEl.textContent = msg;
        });
      }
      showModalError("script-modal-error", data.error || "Submission failed. Check logs.");
    } else {
      closeScriptModal();
      showToast(`✓ Workflow submitted: ${data.workflow_name}`, data.argo_url);
    }
  } catch (err) {
    showModalError("script-modal-error", `Network error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Script";
  }
}

function closeScriptModal() {
  closeOverlay(scriptOverlay);
  _currentScript = null;
}

// ── Upload modal ──────────────────────────────────────────────────────────────
const uploadOverlay = document.getElementById("upload-modal-overlay");
let _argRows = [];

function openUploadModal() {
  _argRows = [];
  document.getElementById("args-builder").innerHTML = "";
  ["u-name","u-team","u-language","u-description","u-deps","u-cpu","u-memory"].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.value = el.tagName === "SELECT" ? "" : (id === "u-cpu" ? "200m" : id === "u-memory" ? "256Mi" : ""); }
  });
  // Clear file inputs explicitly (browsers don't reset these with .value = "")
  ["u-script-file","u-logo"].forEach(id => {
    const el = document.getElementById(id);
    if (el) { try { el.value = ""; } catch(_) {} }
  });
  // Reset script file hint
  const hint = document.getElementById("u-script-file-hint");
  if (hint) hint.textContent = "Select a .py, .sh or .ps1 file";
  const approvalEl = document.getElementById("u-approval");
  if (approvalEl) approvalEl.checked = true;
  updateApprovalLabel();
  clearAllUploadErrors();
  clearElement("upload-modal-error");
  openOverlay(uploadOverlay);
}

function closeUploadModal() { closeOverlay(uploadOverlay); }

// Approval toggle label
document.getElementById("u-approval")?.addEventListener("change", updateApprovalLabel);
function updateApprovalLabel() {
  const el = document.getElementById("u-approval");
  const label = document.getElementById("u-approval-label");
  if (el && label) label.textContent = el.checked ? "Yes" : "No";
}

// Language → file hint
document.getElementById("u-language")?.addEventListener("change", () => {
  const lang = document.getElementById("u-language")?.value;
  const hint = document.getElementById("u-script-file-hint");
  if (hint) {
    const map = { python: ".py", bash: ".sh", powershell: ".ps1" };
    hint.textContent = lang ? `File must be a ${map[lang]} file` : "Select a .py, .sh or .ps1 file";
  }
});

// Args builder
function addArgRow() {
  const idx = _argRows.length;
  _argRows.push(idx);
  const builder = document.getElementById("args-builder");
  const row = document.createElement("div");
  row.className = "arg-row";
  row.id = `arg-row-${idx}`;
  row.innerHTML = `
    <div class="arg-col"><div class="arg-label">Name</div>
      <input class="form-input" id="arg-name-${idx}" type="text" placeholder="my-arg"/></div>
    <div class="arg-col"><div class="arg-label">Type</div>
      <select class="form-input" id="arg-type-${idx}" onchange="toggleArgExtras(${idx})">
        <option value="string">string</option>
        <option value="integer">integer</option>
        <option value="boolean">boolean</option>
        <option value="select">select</option>
      </select></div>
    <div class="arg-col" id="arg-minmax-${idx}" style="display:none"><div class="arg-label">Min</div>
      <input class="form-input" id="arg-min-${idx}" type="number" placeholder="—"/></div>
    <div class="arg-col" id="arg-maxcol-${idx}" style="display:none"><div class="arg-label">Max</div>
      <input class="form-input" id="arg-max-${idx}" type="number" placeholder="—"/></div>
    <div class="arg-col" id="arg-unitcol-${idx}" style="display:none"><div class="arg-label">Unit</div>
      <input class="form-input" id="arg-unit-${idx}" type="text" placeholder="days"/></div>
    <div class="arg-col" id="arg-optcol-${idx}" style="display:none">
      <div class="arg-label">
        Options
        <label style="font-size:0.6rem;margin-left:8px;font-weight:400;text-transform:none;letter-spacing:0;cursor:pointer">
          <input type="checkbox" id="arg-dep-${idx}" onchange="toggleDependsOn(${idx})" style="width:12px;height:12px;vertical-align:middle"/>
          depends on another arg
        </label>
      </div>
      <div id="arg-dep-wrap-${idx}" style="display:none;margin-bottom:6px">
        <input class="form-input" id="arg-dep-name-${idx}" type="text" placeholder="parent-arg-name"
               style="margin-bottom:4px"/>
        <div class="field-hint">Then write options as:<br/>
          <code>dev: proj-a, proj-b</code><br/>
          <code>prod: proj-x, proj-y</code>
        </div>
      </div>
      <textarea class="form-input" id="arg-options-${idx}" rows="5"
                placeholder="prod, staging, dev" style="resize:vertical;font-size:0.8rem;min-height:100px;max-height:400px;width:100%"></textarea>
    </div>
    <div class="arg-col"><div class="arg-label">Example</div>
      <input class="form-input" id="arg-example-${idx}" type="text" placeholder="e.g. value"/></div>
    <div class="arg-col arg-col-checkbox"><div class="arg-label">Required</div>
      <input type="checkbox" id="arg-req-${idx}" class="arg-checkbox"/></div>
    <button class="remove-arg-btn" onclick="removeArgRow(${idx})" title="Remove">✕</button>`;
  builder.appendChild(row);
}

function toggleArgExtras(idx) {
  const type      = document.getElementById(`arg-type-${idx}`)?.value;
  const minmaxCol = document.getElementById(`arg-minmax-${idx}`);
  const maxCol    = document.getElementById(`arg-maxcol-${idx}`);
  const unitCol   = document.getElementById(`arg-unitcol-${idx}`);
  const optCol    = document.getElementById(`arg-optcol-${idx}`);

  if (minmaxCol) minmaxCol.style.display = type === "integer" ? "" : "none";
  if (maxCol)    maxCol.style.display    = type === "integer" ? "" : "none";
  if (unitCol)   unitCol.style.display   = type === "integer" ? "" : "none";
  if (optCol)    optCol.style.display    = type === "select"  ? "" : "none";

  if (type !== "integer") {
    const minEl = document.getElementById(`arg-min-${idx}`);
    const maxEl = document.getElementById(`arg-max-${idx}`);
    if (minEl) minEl.value = "";
    if (maxEl) maxEl.value = "";
  }
  if (type !== "select") {
    const optEl = document.getElementById(`arg-options-${idx}`);
    if (optEl) optEl.value = "";
  }
}

function toggleDependsOn(idx) {
  const checked = document.getElementById(`arg-dep-${idx}`)?.checked;
  const wrap    = document.getElementById(`arg-dep-wrap-${idx}`);
  const textarea = document.getElementById(`arg-options-${idx}`);
  if (wrap) wrap.style.display = checked ? "" : "none";
  if (textarea) {
    textarea.placeholder = checked
      ? "dev: proj-a, proj-b\npp: proj-x\nprod: proj-alpha, proj-beta"
      : "prod, staging, dev";
    textarea.value = "";
  }
}

function removeArgRow(idx) {
  document.getElementById(`arg-row-${idx}`)?.remove();
  _argRows = _argRows.filter(i => i !== idx);
}

function buildArgsJson() {
  const args = [];
  const argNameRe = /^[a-z0-9]+(-[a-z0-9]+)*$/;
  const errors = [];

  for (const idx of _argRows) {
    if (!document.getElementById(`arg-row-${idx}`)) continue;
    const name    = (document.getElementById(`arg-name-${idx}`)?.value || "").trim();
    const type    = document.getElementById(`arg-type-${idx}`)?.value || "string";
    const min     = document.getElementById(`arg-min-${idx}`)?.value;
    const max     = document.getElementById(`arg-max-${idx}`)?.value;
    const unit    = (document.getElementById(`arg-unit-${idx}`)?.value || "").trim();
    const optRaw  = (document.getElementById(`arg-options-${idx}`)?.value || "").trim();
    const example = (document.getElementById(`arg-example-${idx}`)?.value || "").trim();
    const req     = document.getElementById(`arg-req-${idx}`)?.checked || false;

    if (!name) { errors.push(`Arg #${idx+1}: name is required`); continue; }
    if (!argNameRe.test(name)) { errors.push(`Arg #${idx+1}: name "${name}" must be kebab-case (e.g. my-arg)`); continue; }

    if (type === "select" && !optRaw) {
      errors.push(`Arg "${name}": select type requires at least one option`); continue;
    }

    const arg = { name, type, required: req };
    if (type === "integer") {
      if (min !== "" && min !== null && min !== undefined) arg.min = Number(min);
      if (max !== "" && max !== null && max !== undefined) arg.max = Number(max);
      if (unit) arg.unit = unit;
    }
    if (type === "select" && optRaw) {
      const isDependent = document.getElementById(`arg-dep-${idx}`)?.checked;
      const depName     = (document.getElementById(`arg-dep-name-${idx}`)?.value || "").trim();
      if (isDependent && depName) {
        // Parse dict format: "dev: proj-a, proj-b" / "prod: proj-x" (one per line)
        arg.depends_on = depName;
        arg.options    = {};
        optRaw.split("\n").forEach(line => {
          const colonIdx = line.indexOf(":");
          if (colonIdx === -1) return;
          const key  = line.slice(0, colonIdx).trim();
          const vals = line.slice(colonIdx + 1).split(",").map(v => v.trim()).filter(Boolean);
          if (key && vals.length) arg.options[key] = vals;
        });
        if (!Object.keys(arg.options).length) {
          errors.push(`Arg "${name}": no valid options parsed. Format: "key: val1, val2"`);
          continue;
        }
      } else {
        arg.options = optRaw.split(/[,\n]/).map(o => o.trim()).filter(Boolean);
      }
    }
    if (example) arg.example = example;
    args.push(arg);
  }
  return { args, errors };
}

// Upload field validation
const SCRIPT_NAME_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function validateUploadField(fieldId) {
  const el = document.getElementById(fieldId);
  if (!el) return true;
  const val = el.value?.trim() || "";
  let error = "";

  switch (fieldId) {
    case "u-name":
      if (!val) error = "Script name is required";
      else if (!SCRIPT_NAME_RE.test(val)) error = "Use lowercase letters, numbers and hyphens only (e.g. my-script)";
      else if (val.length > 60) error = "Max 60 characters";
      break;
    case "u-team":
      if (!val) error = "Team is required";
      break;
    case "u-language":
      if (!val) error = "Language is required";
      // Update file hint
      const hint = document.getElementById("u-script-file-hint");
      if (hint) {
        const map = { python: ".py", bash: ".sh", powershell: ".ps1" };
        hint.textContent = val ? `File must be a ${map[val]} file` : "Select a .py, .sh or .ps1 file";
      }
      break;
    case "u-description":
      if (!val) error = "Description is required";
      else if (val.length > 200) error = `Max 200 chars (${val.length} used)`;
      break;
    case "u-script-file":
      const lang = document.getElementById("u-language")?.value;
      if (el.files && el.files[0]) {
        if (lang) {
          const extMap = { python: ".py", bash: ".sh", powershell: ".ps1" };
          const expected = extMap[lang] || "";
          if (expected && !el.files[0].name.endsWith(expected)) {
            error = `For ${lang}, file must end in ${expected}`;
          }
        }
      } else {
        // File was cleared — clear any previous error
        error = "";
      }
      break;
    case "u-logo":
      if (el.files && el.files[0]) {
        const logoName = el.files[0].name.toLowerCase();
        const validLogoExt = [".png", ".jpg", ".jpeg"].some(e => logoName.endsWith(e));
        if (!validLogoExt) error = "Must be a .png, .jpg or .jpeg file";
        else if (el.files[0].size > 2 * 1024 * 1024) error = "Max 2MB";
      }
      break;
  }

  const errEl = document.getElementById(`${fieldId}-err`);
  if (errEl) errEl.textContent = error;
  el.classList.toggle("error", !!error);
  return !error;
}

async function submitUpload() {
  const btn = document.getElementById("upload-submit-btn");

  // Validate all fields
  const fieldsToValidate = ["u-name","u-team","u-language","u-description","u-script-file","u-logo"];
  let valid = fieldsToValidate.map(f => validateUploadField(f)).every(Boolean);

  // Validate deps
  const depsEl = document.getElementById("u-deps");
  const depsVal = depsEl?.value?.trim() || "";
  const depRe = /^[a-zA-Z0-9_\-\.]+$/;
  if (depsVal) {
    const badDep = depsVal.split(",").map(d => d.trim()).filter(d => d && !depRe.test(d))[0];
    if (badDep) {
      document.getElementById("u-deps-err").textContent = `Invalid package name: "${badDep}"`;
      valid = false;
    }
  }

  // Validate args
  const { args: parsedArgs, errors: argErrors } = buildArgsJson();
  if (argErrors.length > 0) {
    document.getElementById("u-args-err").textContent = argErrors[0];
    valid = false;
  } else {
    document.getElementById("u-args-err").textContent = "";
  }

  if (!valid) {
    showModalError("upload-modal-error", "Please fix the errors above.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Creating MR…";
  clearElement("upload-modal-error");

  const formData = new FormData();
  formData.append("script_name",       document.getElementById("u-name").value.trim());
  formData.append("team",              document.getElementById("u-team").value);
  formData.append("language",          document.getElementById("u-language").value);
  formData.append("description",       document.getElementById("u-description").value.trim());
  formData.append("dependencies",      depsVal);
  formData.append("approval_required", document.getElementById("u-approval").checked ? "true" : "false");
  formData.append("args",              JSON.stringify(parsedArgs));
  formData.append("resources_cpu",     document.getElementById("u-cpu").value.trim() || "200m");
  formData.append("resources_memory",  document.getElementById("u-memory").value.trim() || "256Mi");
  formData.append("namespace",         document.getElementById("u-namespace")?.value?.trim() || "");

  const scriptFile = document.getElementById("u-script-file").files[0];
  const logoFile   = document.getElementById("u-logo").files[0];
  if (scriptFile) formData.append("script_file", scriptFile);
  if (logoFile)   formData.append("logo", logoFile);

  try {
    const resp = await fetch("/api/scripts/upload", { method: "POST", body: formData });
    const data = await resp.json();

    if (!resp.ok) {
      if (data.field_errors) {
        Object.entries(data.field_errors).forEach(([field, msg]) => {
          const idMap = {
            script_name: "u-name", team: "u-team", language: "u-language",
            description: "u-description", script_file: "u-script-file",
            logo: "u-logo", dependencies: "u-deps", args: "u-args",
          };
          const errId = idMap[field];
          if (errId) document.getElementById(`${errId}-err`).textContent = msg;
        });
      }
      showModalError("upload-modal-error", data.error || "Upload failed.");
    } else {
      closeUploadModal();
      showToast("✓ MR created! Awaiting team approval.", data.mr_url);
    }
  } catch (err) {
    showModalError("upload-modal-error", `Network error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Create MR";
  }
}

function clearAllUploadErrors() {
  ["u-name","u-team","u-language","u-description","u-script-file","u-logo","u-deps","u-args"].forEach(id => {
    const errEl = document.getElementById(`${id}-err`);
    if (errEl) errEl.textContent = "";
    const el = document.getElementById(id);
    if (el) el.classList.remove("error");
  });
}

// ── Search ────────────────────────────────────────────────────────────────────
const searchInput   = document.getElementById("search-input");
const searchClear   = document.getElementById("search-clear");
const searchResults = document.getElementById("search-results");

function handleSearch(query) {
  const q = query.trim().toLowerCase();
  searchClear.classList.toggle("visible", q.length > 0);
  if (!q) { searchResults.classList.remove("open"); searchResults.innerHTML = ""; return; }

  const matchedSites   = sitesData.filter(s => s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)).slice(0, 5);
  const matchedApps    = appsData.filter(a => a.display_name.toLowerCase().includes(q)).slice(0, 5);
  const matchedScripts = [];
  for (const [team, scripts] of Object.entries(scriptsByTeam)) {
    for (const s of scripts) {
      if (s.name.toLowerCase().includes(q) || (s.description||"").toLowerCase().includes(q)) {
        matchedScripts.push({ ...s, team });
        if (matchedScripts.length >= 6) break;
      }
    }
    if (matchedScripts.length >= 6) break;
  }

  if (!matchedSites.length && !matchedApps.length && !matchedScripts.length) {
    searchResults.innerHTML = `<div class="search-no-results">No results for "<strong>${esc(q)}</strong>"</div>`;
    searchResults.classList.add("open");
    return;
  }

  let html = "";
  if (matchedSites.length) {
    html += `<div class="search-group-label">Web Apps</div>`;
    matchedSites.forEach(s => {
      const tagHtml = (s.tags||[]).map(t => {
        const p = TAG_PALETTE[_tagHash(t)];
        return `<span class="site-tag" data-tag="${esc(t)}" style="background:${p.bg};color:${p.text};border:1px solid ${p.text}30">${esc(t)}</span>`;
      }).join("");
      html += `<a class="search-result-item" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" onclick="clearSearch()">
        <img class="search-result-img" src="${esc(s.image_url||'/static/icons/_placeholder.svg')}" alt="${esc(s.name)}" onerror="this.src='/static/icons/_placeholder.svg'"/>
        <div class="search-result-info">
          <span class="search-result-name">${highlight(s.name,q)}</span>
          <span class="search-result-sub">${esc(s.url)}</span>
          ${tagHtml ? `<div class="site-tags" style="margin-top:3px">${tagHtml}</div>` : ""}
        </div>
        <span class="search-result-badge badge-web">web</span></a>`;
    });
  }
  if (matchedApps.length) {
    html += `<div class="search-group-label">App Downloads</div>`;
    matchedApps.forEach(a => {
      const idx = appsData.indexOf(a);
      html += `<div class="search-result-item" onclick="searchOpenApp(${idx})">
        <img class="search-result-img" src="${esc(a.icon_url||'/static/icons/_placeholder.svg')}" alt="${esc(a.display_name)}" onerror="this.src='/static/icons/_placeholder.svg'"/>
        <div class="search-result-info"><span class="search-result-name">${highlight(a.display_name,q)}</span><span class="search-result-sub">${a.versions?.length||0} version(s)</span></div>
        <span class="search-result-badge badge-app">install</span></div>`;
    });
  }
  if (matchedScripts.length) {
    html += `<div class="search-group-label">Scripts</div>`;
    matchedScripts.forEach(s => {
      html += `<div class="search-result-item" onclick="clearSearch();openScriptModal('${esc(s.team)}','${esc(s.folder_name)}')">
        <img class="search-result-img" src="/api/scripts/${esc(s.team)}/${esc(s.folder_name)}/logo" alt="${esc(s.name)}" onerror="this.src='/static/icons/_placeholder.svg'"/>
        <div class="search-result-info"><span class="search-result-name">${highlight(s.name,q)}</span><span class="search-result-sub">${esc(s.team)} · ${esc(s.language)}</span></div>
        <span class="search-result-badge badge-script">script</span></div>`;
    });
  }

  searchResults.innerHTML = html;
  searchResults.classList.add("open");
}

function searchOpenApp(index) { clearSearch(); openVersionModal(index); }
function clearSearch() {
  searchInput.value = "";
  searchClear.classList.remove("visible");
  searchResults.classList.remove("open");
  searchResults.innerHTML = "";
}
document.addEventListener("click", e => { if (!e.target.closest(".search-wrap")) searchResults.classList.remove("open"); });
searchInput?.addEventListener("focus", () => { if (searchInput.value.trim()) handleSearch(searchInput.value); });

function highlight(text, query) {
  const safe  = esc(text);
  const safeQ = esc(query).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(safeQ, "gi"), m => `<mark style="background:var(--accent-glow);color:var(--accent);border-radius:2px">${m}</mark>`);
}

// ── Toast notification ────────────────────────────────────────────────────────
function showToast(message, linkUrl) {
  const existing = document.getElementById("eden-toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.id = "eden-toast";
  toast.style.cssText = `
    position:fixed;bottom:80px;right:28px;z-index:600;
    background:var(--surface);border:1px solid var(--accent);
    border-radius:10px;padding:14px 18px;
    font-family:var(--font-mono);font-size:0.75rem;color:var(--text);
    box-shadow:0 8px 32px rgba(0,0,0,.5);max-width:360px;
    animation:fadeUp .3s ease both;`;
  toast.innerHTML = `<div style="color:var(--accent);margin-bottom:4px">${esc(message)}</div>
    ${linkUrl ? `<a href="${esc(linkUrl)}" target="_blank" style="color:var(--accent2);text-decoration:underline">Open in browser ↗</a>` : ""}`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 7000);
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openOverlay(overlay) {
  overlay.classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeOverlay(overlay) {
  overlay.classList.remove("open");
  document.body.style.overflow = "";
}
function showModalError(id, msg) {
  const el = document.getElementById(id);
  if (el) { el.textContent = msg; el.classList.add("visible"); }
}
function clearElement(id) {
  const el = document.getElementById(id);
  if (el) { el.textContent = ""; el.classList.remove("visible"); }
}

document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  closeModal();
  closeScriptModal();
  closeUploadModal();
  clearSearch();
});

// ── Live refresh ──────────────────────────────────────────────────────────────
async function refreshAppsData() {
  try {
    const res = await fetch("/api/apps");
    if (res.ok) appsData = await res.json();
  } catch (_) {}
}
// ── Script card rendering ─────────────────────────────────────────────────────

function _buildScriptCard(team, script) {
  const desc = script.description || "";
  const shortDesc = desc.length > 50 ? desc.slice(0, 50) + "..." : desc;
  const approvalBadge = script.approval_required
    ? `<span class="approval-badge">approval</span>` : "";
  return `
    <button class="script-card"
            onclick="openScriptModal('${esc(team)}', '${esc(script.folder_name)}')"
            data-name="${esc((script.name || "").toLowerCase())}"
            data-desc="${esc(desc.toLowerCase())}"
            data-team="${esc(team)}">
      <div class="card-img-wrap">
        <img src="/api/scripts/${esc(team)}/${esc(script.folder_name)}/logo"
             alt="${esc(script.name)}"
             onerror="this.src='/static/icons/_placeholder.svg'"/>
      </div>
      <div class="card-label">
        <span class="card-name">${esc(script.name)}</span>
        <span class="card-meta">${esc(shortDesc)}</span>
        <div class="script-badges">
          <span class="lang-badge lang-${esc(script.language)}">${esc(script.language)}</span>
          ${approvalBadge}
        </div>
      </div>
    </button>`;
}

function _renderTeamScripts(team, scripts) {
  const outer = document.getElementById(`scripts-outer-${team}`);
  if (!outer) return;

  // Update count badge in section header
  const section = document.getElementById(`section-team-${team}`);
  if (section) {
    const countEl = section.querySelector(".section-count");
    if (countEl) countEl.textContent = scripts.length;
  }

  if (!scripts || scripts.length === 0) {
    outer.innerHTML = `
      <div class="empty-state">
        <span>No scripts yet for <strong>${esc(team)}</strong></span>
      </div>`;
    return;
  }

  outer.innerHTML = `
    <div class="cards-row">
      ${scripts.map(s => _buildScriptCard(team, s)).join("")}
    </div>`;
}

async function refreshScriptsData() {
  try {
    const res = await fetch("/api/scripts");
    if (!res.ok) return;
    const fresh = await res.json();

    // Re-render any team whose script count changed
    for (const [team, scripts] of Object.entries(fresh)) {
      const current = scriptsByTeam[team] || [];
      if (scripts.length !== current.length) {
        _renderTeamScripts(team, scripts);
      }
    }

    scriptsByTeam = fresh;
  } catch (_) {}
}

setInterval(refreshAppsData,    60 * 1000);
setInterval(refreshScriptsData, 15 * 1000);  // check every 15 seconds

// ── Theme toggle ──────────────────────────────────────────────────────────────
const THEME_KEY = "eden-theme";
function applyTheme(theme) {
  const icon  = document.getElementById("theme-icon");
  const label = document.getElementById("theme-label");
  if (theme === "light") {
    document.body.classList.add("theme-light");
    document.documentElement.classList.add("theme-light");
    if (icon)  icon.textContent  = "🌑";
    if (label) label.textContent = "Dark";
  } else {
    document.body.classList.remove("theme-light");
    document.documentElement.classList.remove("theme-light");
    if (icon)  icon.textContent  = "☀️";
    if (label) label.textContent = "Light";
  }
}
function toggleTheme() {
  const next = document.body.classList.contains("theme-light") ? "dark" : "light";
  try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  applyTheme(next);
}
// Apply saved theme on DOMContentLoaded so elements are guaranteed to exist
document.addEventListener("DOMContentLoaded", () => {
  let saved = "dark";
  try { saved = localStorage.getItem(THEME_KEY) || "dark"; } catch (_) {}
  // Migrate stale "pink" key from old versions
  if (saved === "pink") { saved = "light"; try { localStorage.setItem(THEME_KEY, "light"); } catch(_){} }
  applyTheme(saved);
});