import React, { useState, useCallback, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import toast from 'react-hot-toast';
import { ScriptCard } from '../../components/ScriptCard/ScriptCard.jsx';
import { Modal }      from '../../components/Modal/Modal.jsx';
import { Button }     from '../../components/Button/Button.jsx';
import {
  submitScript, uploadScript, uploadScriptArgFile,
  fetchPendingScripts, approveScript, rejectScript,
} from '../../api/index.js';
import styles from './ScriptsPage.module.css';

// ── Run modal ─────────────────────────────────────────────────────────────────
function RunModal({ script, team, open, onClose }) {
  const [args,    setArgs]    = useState({});
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState('');
  // js_file args go through a separate upload step before they can be part
  // of `args` at all — this tracks that per-arg-name: 'uploading' | 'done' | 'error'.
  const [fileStatus, setFileStatus] = useState({});
  // Bumped on every reset so file inputs remount (see fileInputKey below) —
  // native <input type="file"> can't be cleared by just changing React state,
  // the browser keeps showing the previously-chosen filename unless the
  // element itself is recreated.
  const [resetKey, setResetKey] = useState(0);

  // RunModal is always mounted (ScriptsPage renders it unconditionally and
  // only its `open` prop toggles) — so without this, args/error/upload
  // status from the last run would still be sitting here next time it opens.
  useEffect(() => {
    if (!open) return;
    setArgs({});
    setError('');
    setLoading(false);
    setFileStatus({});
    setResetKey(k => k + 1);
  }, [open, script]);

  const anyFileUploading = Object.values(fileStatus).some(s => s.state === 'uploading');

  const handleFileArgChange = async (arg, file) => {
    const name = arg.name;
    if (!file) {
      // Cleared — drop both the stored value and the status
      setArgs(p => { const n = { ...p }; delete n[name]; return n; });
      setFileStatus(p => { const n = { ...p }; delete n[name]; return n; });
      return;
    }
    if (!file.name.toLowerCase().endsWith('.js')) {
      setFileStatus(p => ({ ...p, [name]: { state: 'error', message: 'Only .js files are accepted', filename: file.name } }));
      return;
    }
    setFileStatus(p => ({ ...p, [name]: { state: 'uploading', filename: file.name } }));
    try {
      // The endpoint is a stateless converter — nothing is stored server-side,
      // it just hands back the file's base64 content, which becomes the arg's
      // real value directly (no separate reference to resolve at submit time).
      const result = await uploadScriptArgFile(file);
      setArgs(p => ({ ...p, [name]: result.value }));
      setFileStatus(p => ({ ...p, [name]: { state: 'done', filename: file.name } }));
    } catch (e) {
      setArgs(p => { const n = { ...p }; delete n[name]; return n; });
      setFileStatus(p => ({ ...p, [name]: { state: 'error', message: e.message, filename: file.name } }));
    }
  };

  const handleRun = async () => {
    setError('');
    setLoading(true);
    try {
      await submitScript({ team, script_name: script.folder_name, args });
      toast.success(script.approval_required
        ? 'Script submitted. Contact Genesys team for approval.'
        : 'Script submitted!');
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const updateArg = (name, value) => setArgs(p => ({ ...p, [name]: value }));

  const getChildOptions = (arg) => {
    const parent = (script.args || []).find(a => a.name === arg.depends_on);
    const parentVal = parent ? (args[parent.name] || '') : '';
    if (!parentVal) return [];
    return (arg.options || {})[parentVal] || [];
  };

  if (!script) return null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={script.name}
      subtitle={script.description}
      confirmLeave
      footer={
        <>
          {error && <span className={styles.runError}>{error}</span>}
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button loading={loading} disabled={anyFileUploading} onClick={handleRun}>Run Script</Button>
        </>
      }
    >
      <div className={styles.runForm}>
        {(script.args || []).map(arg => {
          const isDependent = !!arg.depends_on;
          const parentVal = isDependent ? (args[arg.depends_on] || '') : null;
          const childOpts = isDependent ? getChildOptions(arg) : [];
          const disabled  = isDependent && !parentVal;

          return (
            <div key={arg.name} className={styles.runField}>
              <label className={styles.runLabel}>
                {arg.name.replace(/-/g,' ')}
                {arg.required && <span className={styles.req}> *required</span>}
                {arg.depends_on && <span className={styles.dep}> depends on {arg.depends_on}</span>}
              </label>

              {arg.type === 'boolean' ? (
                <label className={styles.toggle}>
                  <input type="checkbox"
                         checked={args[arg.name] === 'true'}
                         onChange={e => updateArg(arg.name, e.target.checked ? 'true' : 'false')} />
                  <span className={styles.toggleSlider} />
                </label>
              ) : arg.type === 'js_file' ? (
                <>
                  <input
                    key={`${arg.name}-${resetKey}`}
                    className={styles.runInput}
                    type="file"
                    accept=".js"
                    onChange={e => handleFileArgChange(arg, e.target.files?.[0] || null)}
                  />
                  {fileStatus[arg.name]?.state === 'uploading' && (
                    <span className={styles.fileStatus}>
                      Uploading {fileStatus[arg.name].filename}…
                    </span>
                  )}
                  {fileStatus[arg.name]?.state === 'done' && (
                    <span className={`${styles.fileStatus} ${styles.fileStatusDone}`}>
                      ✓ {fileStatus[arg.name].filename} uploaded
                    </span>
                  )}
                  {fileStatus[arg.name]?.state === 'error' && (
                    <span className={`${styles.fileStatus} ${styles.fileStatusError}`}>
                      {fileStatus[arg.name].message}
                    </span>
                  )}
                </>
              ) : arg.type === 'select' ? (
                <select
                  className={styles.runInput}
                  value={args[arg.name] || ''}
                  disabled={disabled}
                  onChange={e => {
                    updateArg(arg.name, e.target.value);
                    // Clear children that depend on this
                    (script.args || []).forEach(a => {
                      if (a.depends_on === arg.name) updateArg(a.name, '');
                    });
                  }}
                >
                  <option value="">{disabled ? `Select ${arg.depends_on} first` : `-- select ${arg.name} --`}</option>
                  {(isDependent ? childOpts : (arg.options || [])).map(opt => (
                    <option key={opt} value={opt}>{opt}</option>
                  ))}
                </select>
              ) : (
                <input
                  className={styles.runInput}
                  type={arg.type === 'integer' ? 'number' : 'text'}
                  value={args[arg.name] || ''}
                  placeholder={arg.example || ''}
                  onChange={e => updateArg(arg.name, e.target.value)}
                />
              )}

              {arg.unit && <span className={styles.unit}>{arg.unit}</span>}
            </div>
          );
        })}
        {(!script.args || script.args.length === 0) && (
          <p className={styles.noArgs}>This script has no configurable arguments.</p>
        )}
      </div>
    </Modal>
  );
}

// ── Upload modal ──────────────────────────────────────────────────────────────
const UPLOAD_INITIAL_FORM = { script_name:'', team:'', language:'', description:'', resources_cpu:'200m', resources_memory:'256Mi', dependencies:'', approval_required:true };

function UploadModal({ open, onClose, teams }) {
  // Keys here must match exactly what backend's /api/scripts/upload reads
  // via request.form.get(...) — handleUpload below spreads this object
  // straight into the FormData using these keys as the field names. They
  // used to be shorthand (name/deps/cpu/memory/approval) that didn't match
  // the backend's (script_name/dependencies/resources_cpu/resources_memory/
  // approval_required), so script_name was always empty and every upload
  // failed validation with a 422.
  const [form,    setForm]    = useState(UPLOAD_INITIAL_FORM);
  const [scriptFile, setScriptFile] = useState(null);
  const [logoFile,   setLogoFile]   = useState(null);
  const [loading, setLoading] = useState(false);
  // Bumped on every reset to remount the two file inputs — see RunModal's
  // resetKey above for why that's needed (React state alone can't clear a
  // native file input's displayed filename).
  const [resetKey, setResetKey] = useState(0);

  // Same issue as RunModal: this component is always mounted, only `open`
  // toggles, so without this a closed-then-reopened form would still show
  // the last MR's fields and picked files.
  useEffect(() => {
    if (!open) return;
    setForm(UPLOAD_INITIAL_FORM);
    setScriptFile(null);
    setLogoFile(null);
    setLoading(false);
    setResetKey(k => k + 1);
  }, [open]);

  const f = (k) => (e) => setForm(p => ({ ...p, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }));

  const handleUpload = async () => {
    if (!form.script_name || !form.team || !form.language || !form.description || !scriptFile) {
      toast.error('Please fill all required fields and attach a script file');
      return;
    }
    setLoading(true);
    try {
      const fd = new FormData();
      Object.entries(form).forEach(([k,v]) => fd.append(k, v));
      fd.append('script_file', scriptFile);
      if (logoFile) fd.append('logo', logoFile);
      const result = await uploadScript(fd);
      toast.success('MR created! ' + (result.mr_url ? `View: ${result.mr_url}` : ''));
      onClose();
    } catch (e) { toast.error(e.message); }
    finally { setLoading(false); }
  };

  return (
    <Modal open={open} onClose={onClose} wide title="Upload New Script"
           subtitle="Creates a GitLab MR for team approval" confirmLeave
           footer={
             <>
               <Button variant="secondary" onClick={onClose}>Cancel</Button>
               <Button loading={loading} onClick={handleUpload}>Create MR</Button>
             </>
           }>
      <div className={styles.uploadForm}>
        <div className={styles.row}>
          <FormGroup label="Script Name *">
            <input className={styles.input} value={form.script_name} onChange={f('script_name')} placeholder="my-script" />
          </FormGroup>
          <FormGroup label="Team *">
            <select className={styles.input} value={form.team} onChange={f('team')}>
              <option value="">Select team…</option>
              {teams.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </FormGroup>
        </div>
        <div className={styles.row}>
          <FormGroup label="Language *">
            <select className={styles.input} value={form.language} onChange={f('language')}>
              <option value="">Select…</option>
              <option value="python">Python</option>
              <option value="bash">Bash</option>
              <option value="powershell">PowerShell</option>
            </select>
          </FormGroup>
          <FormGroup label="Approval Required">
            <label className={styles.toggleRow}>
              <input type="checkbox" checked={form.approval_required} onChange={f('approval_required')} />
              <span>{form.approval_required ? 'Yes' : 'No'}</span>
            </label>
          </FormGroup>
        </div>
        <FormGroup label="Description *">
          <input className={styles.input} value={form.description} onChange={f('description')} placeholder="What does this script do?" />
        </FormGroup>
        <div className={styles.row}>
          <FormGroup label="Script File *">
            <input key={`script-file-${resetKey}`} className={styles.input} type="file" accept=".py,.sh,.ps1"
                   onChange={e => setScriptFile(e.target.files[0])} />
          </FormGroup>
          <FormGroup label="Logo (optional)">
            <input key={`logo-file-${resetKey}`} className={styles.input} type="file" accept=".png,.jpg,.jpeg"
                   onChange={e => setLogoFile(e.target.files[0])} />
          </FormGroup>
        </div>
        <div className={styles.row}>
          <FormGroup label="CPU">
            <input className={styles.input} value={form.resources_cpu} onChange={f('resources_cpu')} placeholder="200m" />
          </FormGroup>
          <FormGroup label="Memory">
            <input className={styles.input} value={form.resources_memory} onChange={f('resources_memory')} placeholder="256Mi" />
          </FormGroup>
        </div>
        <FormGroup label="Dependencies">
          <input className={styles.input} value={form.dependencies} onChange={f('dependencies')} placeholder="kubernetes, hvac" />
        </FormGroup>
      </div>
    </Modal>
  );
}

// ── Pending scripts modal ─────────────────────────────────────────────────────
function PendingScriptsModal({ open, onClose }) {
  const [list,    setList]    = useState([]);
  const [loading, setLoading] = useState(false);

  React.useEffect(() => {
    if (!open) return;
    setLoading(true);
    fetchPendingScripts()
      .then(setList)
      .catch(() => toast.error('Failed to load'))
      .finally(() => setLoading(false));
  }, [open]);

  const handle = async (id, approve) => {
    try {
      await (approve ? approveScript(id) : rejectScript(id));
      toast.success(approve ? 'MR merged!' : 'Rejected');
      setList(prev => prev.filter(s => s.id !== id));
    } catch (e) { toast.error(e.message); }
  };

  return (
    <Modal open={open} onClose={onClose} wide title="Pending Script Submissions"
           footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      {loading ? <p className={styles.loading}>Loading…</p> :
       list.length === 0 ? <p className={styles.empty}>No pending scripts</p> :
       list.map(s => (
         <div key={s.id} className={styles.pendingItem}>
           <div className={styles.pendingInfo}>
             <span className={styles.pendingName}>{s.script_name}</span>
             <span className={styles.pendingMeta}>{s.team} · {s.language} · by {s.submitted_by}</span>
             {s.mr_url && <a href={s.mr_url} target="_blank" rel="noreferrer" className={styles.mrLink}>View MR ↗</a>}
           </div>
           <div className={styles.pendingActions}>
             <Button size="sm" onClick={() => handle(s.id, true)}>Approve &amp; Merge</Button>
             <Button size="sm" variant="danger" onClick={() => handle(s.id, false)}>Reject</Button>
           </div>
         </div>
       ))
      }
    </Modal>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
export function ScriptsPage({ scripts, isAdmin }) {
  const [runScript,    setRunScript]    = useState(null);
  const [runTeam,      setRunTeam]      = useState('');
  const [uploadOpen,   setUploadOpen]   = useState(false);
  const [pendingOpen,  setPendingOpen]  = useState(false);
  const location = useLocation();

  const teams = Object.keys(scripts);

  const openRun = useCallback((script, team) => {
    setRunScript(script);
    setRunTeam(team);
  }, []);

  // Open run modal if navigated here from search result
  useEffect(() => {
    const { openScript } = location.state || {};
    if (!openScript || !Object.keys(scripts).length) return;
    // Find the script in the loaded scripts
    const team = openScript.team;
    const found = (scripts[team] || []).find(s => s.folder_name === openScript.folder_name);
    if (found) {
      setRunScript(found);
      setRunTeam(team);
      // Clear state so re-renders don't re-open modal
      window.history.replaceState({}, '');
    }
  }, [location.state, scripts]);

  return (
    <div className={styles.page}>
      {teams.length === 0 && (
        <div className={styles.empty}>
          <div className={styles.emptyIcon}>⌘</div>
          <div className={styles.emptyTitle}>No teams configured</div>
          <div className={styles.emptySub}>Set TEAMS in your eden-config ConfigMap</div>
        </div>
      )}

      {teams.map(team => {
        const teamScripts = scripts[team] || [];
        return (
          <section key={team} className={styles.teamSection}>
            <div className={styles.teamHeader}>
              <h2 className={styles.teamTitle}>{team}</h2>
              <span className={styles.teamCount}>{teamScripts.length}</span>
            </div>
            {teamScripts.length === 0 ? (
              <p className={styles.teamEmpty}>No scripts yet for {team}</p>
            ) : (
              <div className={styles.grid}>
                {teamScripts.map((script, i) => (
                  <ScriptCard key={i} script={script} team={team} onClick={openRun} />
                ))}
              </div>
            )}
          </section>
        );
      })}

      {/* Action buttons */}
      <div className={styles.actions}>
        <Button variant="ghost" onClick={() => setUploadOpen(true)}>+ Upload New Script</Button>
        {isAdmin && (
          <Button variant="ghost" onClick={() => setPendingOpen(true)}>⏳ Review Script MRs</Button>
        )}
      </div>

      {/* Modals */}
      <RunModal
        script={runScript}
        team={runTeam}
        open={!!runScript}
        onClose={() => setRunScript(null)}
      />
      <UploadModal
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        teams={teams}
      />
      <PendingScriptsModal
        open={pendingOpen}
        onClose={() => setPendingOpen(false)}
      />
    </div>
  );
}

function FormGroup({ label, children }) {
  return (
    <div className={styles.formGroup}>
      <label className={styles.formLabel}>{label}</label>
      {children}
    </div>
  );
}
