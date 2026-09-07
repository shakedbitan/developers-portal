import React, { useState, useEffect, useRef } from 'react';
import toast from 'react-hot-toast';
import { Modal } from '../Modal/Modal.jsx';
import { Button } from '../Button/Button.jsx';
import { editMyRunArgs, editMySiteSubmission } from '../../api/index.js';
import styles from './MyRequestsModal.module.css';

function KindBadge({ kind }) {
  const label = kind === 'webapp' ? 'Web App' : kind === 'script_mr' ? 'New Script' : 'Script Run';
  return <span className={`${styles.kindBadge} ${styles[`kind_${kind}`]}`}>{label}</span>;
}

// Active-list badge: for a script run that's cleared review, show its live
// workflow phase (Running/Succeeded/...) instead of the now-stale "approved"
// review status -- review just opened the gate, this is what's actually
// happening now. History uses its own simpler finalStatus below instead,
// since everything there is already fully resolved.
function StatusBadge({ item }) {
  if (item.kind === 'script_run' && item.status === 'approved') {
    const phase = item.workflow_phase || 'Running';
    return <span className={`${styles.statusBadge} ${styles.status_running}`}>{phase}</span>;
  }
  return <span className={`${styles.statusBadge} ${styles[`status_${item.status}`] || ''}`}>
    {item.status === 'pending' ? 'Awaiting review' : item.status}
  </span>;
}

function finalStatusLabel(item) {
  if (item.kind === 'script_run') {
    if (item.status === 'rejected') return 'Rejected';
    if (item.workflow_phase) return item.workflow_phase;
    return 'Approved';
  }
  return item.status === 'rejected' ? 'Rejected' : 'Approved';
}

function finalStatusClass(item) {
  const label = finalStatusLabel(item).toLowerCase();
  if (label === 'rejected' || label === 'failed' || label === 'error') return styles.status_rejected;
  if (label === 'succeeded' || label === 'approved') return styles.status_approved;
  return '';
}

function itemTitle(item) {
  return item.kind === 'webapp' ? item.name : item.script_name;
}

function itemMeta(item) {
  if (item.kind === 'webapp') return item.url;
  return `${item.team} · ${item.kind === 'script_run' ? 'run' : item.language}`;
}

function formatArgs(args) {
  const entries = Object.entries(args || {});
  return entries.length ? entries.map(([k, v]) => `${k}=${v}`).join(', ') : null;
}

// Covers string/integer/boolean/select (incl. dependent selects) and shows
// js_file/argo_target args read-only. File-replace-on-edit isn't offered
// here (submitter can just re-submit if they need to swap the file) --
// simplified sibling of ScriptsPage's PendingRunsModal arg editor.
function RunArgsEditor({ item, values, onChange }) {
  const getChildOptions = (arg) => {
    const parent = (item.arg_defs || []).find(a => a.name === arg.depends_on);
    const parentVal = parent ? (values[parent.name] || '') : '';
    if (!parentVal) return [];
    return (arg.options || {})[parentVal] || [];
  };

  if (!(item.arg_defs || []).length) {
    return <p className={styles.noArgs}>This script has no arguments.</p>;
  }

  return (
    <div className={styles.argForm}>
      {item.arg_defs.map(arg => {
        const isDependent = !!arg.depends_on;
        const parentVal   = isDependent ? (values[arg.depends_on] || '') : null;
        const childOpts   = isDependent ? getChildOptions(arg) : [];
        const selDisabled = isDependent && !parentVal;
        return (
          <div key={arg.name} className={styles.argField}>
            <label className={styles.argLabel}>
              {arg.name.replace(/-/g, ' ')}
              {arg.required && <span className={styles.req}> *required</span>}
            </label>
            {arg.type === 'js_file' ? (
              <div className={styles.lockedValue}>File attached<span className={styles.lockedHint}>re-submit to replace</span></div>
            ) : arg.type === 'select' && arg.argo_target ? (
              <div className={styles.lockedValue}>
                {(arg.options || []).find(o => o.name === values[arg.name])?.label || values[arg.name] || '(not set)'}
                <span className={styles.lockedHint}>locked at submission</span>
              </div>
            ) : arg.type === 'boolean' ? (
              <label className={styles.toggle}>
                <input type="checkbox"
                       checked={values[arg.name] === 'true'}
                       onChange={e => onChange(arg.name, e.target.checked ? 'true' : 'false')} />
                <span className={styles.toggleSlider} />
              </label>
            ) : arg.type === 'select' ? (
              <select className={styles.argInput} value={values[arg.name] || ''} disabled={selDisabled}
                      onChange={e => {
                        onChange(arg.name, e.target.value);
                        (item.arg_defs || []).forEach(a => { if (a.depends_on === arg.name) onChange(a.name, ''); });
                      }}>
                <option value="">{selDisabled ? `Select ${arg.depends_on} first` : `-- select ${arg.name} --`}</option>
                {(isDependent ? childOpts : (arg.options || [])).map(opt => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </select>
            ) : (
              <input className={styles.argInput}
                     type={arg.type === 'integer' ? 'number' : 'text'}
                     value={values[arg.name] || ''}
                     onChange={e => onChange(arg.name, e.target.value)} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function SiteFieldsEditor({ values, onChange }) {
  return (
    <div className={styles.argForm}>
      <div className={styles.argField}>
        <label className={styles.argLabel}>Name</label>
        <input className={styles.argInput} value={values.name || ''} onChange={e => onChange('name', e.target.value)} />
      </div>
      <div className={styles.argField}>
        <label className={styles.argLabel}>URL</label>
        <input className={styles.argInput} value={values.url || ''} onChange={e => onChange('url', e.target.value)} />
      </div>
      <div className={styles.argField}>
        <label className={styles.argLabel}>Tags</label>
        <input className={styles.argInput} placeholder="comma, separated"
               value={(values.tags || []).join(', ')}
               onChange={e => onChange('tags', e.target.value.split(',').map(t => t.trim()).filter(Boolean))} />
      </div>
    </div>
  );
}

// Live pod-log stream for one run, via EventSource against the SSE relay
// in app.py (which itself relays Argo's own log-stream endpoint -- see
// argo_client.stream_workflow_logs). Works the same for a still-running
// run (genuinely live) and a finished one in History (Argo just replays
// what it already has, then the stream ends) -- no special-casing needed
// between the two call sites below.
function LogViewer({ runId }) {
  const [lines, setLines]   = useState([]);
  const [status, setStatus] = useState('connecting'); // connecting | open | done | error
  const boxRef = useRef(null);

  useEffect(() => {
    setLines([]);
    setStatus('connecting');
    const es = new EventSource(`/api/my/requests/run/${runId}/logs`);
    es.onopen = () => setStatus('open');
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.content) setLines(prev => [...prev, data.content]);
      } catch { /* ignore a malformed line rather than drop the whole stream */ }
    };
    es.addEventListener('done', () => { setStatus('done'); es.close(); });
    es.onerror = () => {
      // EventSource fires this both for a genuine failure and for the
      // server closing the connection normally -- readyState tells them
      // apart (CLOSED here is expected once `done` should have already
      // fired, or the workflow's pod is simply gone).
      setStatus(prev => (prev === 'done' ? prev : 'error'));
      es.close();
    };
    return () => es.close();
  }, [runId]);

  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines]);

  const statusText = {
    connecting: 'Connecting…',
    open: '● Live',
    done: 'Stream ended',
    error: 'Connection lost — the pod may be gone',
  }[status];

  return (
    <div className={styles.logPanel}>
      <div className={styles.logHeader}>
        <span className={status === 'open' ? styles.logLive : ''}>{statusText}</span>
      </div>
      <pre ref={boxRef} className={styles.logBox}>
        {lines.length ? lines.join('\n') : 'Waiting for output…'}
      </pre>
    </div>
  );
}

function ActiveItem({ item, expanded, editValues, busy, onToggleExpand, onChange, onSave,
                       logsOpen, onToggleLogs }) {
  // Only genuinely still-pending items can be edited -- an approved run is
  // already executing in Argo, a reviewed webapp submission is already
  // resolved. script_mr is never editable -- editing a GitLab MR's actual
  // code isn't something Eden's UI reaches into.
  const editable = item.status === 'pending' && item.kind !== 'script_mr';
  const canShowLogs = item.kind === 'script_run' && item.status === 'approved';
  return (
    <div className={styles.item}>
      <div className={styles.itemHead}>
        <div className={styles.itemInfo}>
          <div className={styles.itemTop}>
            <KindBadge kind={item.kind} />
            <StatusBadge item={item} />
          </div>
          <span className={styles.itemTitle}>{itemTitle(item)}</span>
          <span className={styles.itemMeta}>{itemMeta(item)}</span>
        </div>
        {canShowLogs && (
          <Button size="sm" variant="ghost" onClick={onToggleLogs}>{logsOpen ? 'Hide Logs' : 'Logs'}</Button>
        )}
        {editable && (
          <Button size="sm" variant="ghost" onClick={onToggleExpand}>{expanded ? 'Hide' : 'Edit'}</Button>
        )}
        {item.mr_url && (
          <a href={item.mr_url} target="_blank" rel="noreferrer" className={styles.mrLink}>View MR ↗</a>
        )}
      </div>

      {expanded && (
        <div className={styles.editPanel}>
          {item.kind === 'script_run'
            ? <RunArgsEditor item={item} values={editValues} onChange={onChange} />
            : <SiteFieldsEditor values={editValues} onChange={onChange} />}
          <Button size="sm" loading={busy} onClick={onSave}>Save Changes</Button>
        </div>
      )}
      {logsOpen && <LogViewer runId={item.id} />}
    </div>
  );
}

function HistoryItem({ item, logsOpen, onToggleLogs }) {
  const argsText = item.kind === 'script_run' ? formatArgs(item.args) : null;
  // Argo's own pod/log retention (podGC/ttlStrategy) means an old run's
  // logs may simply no longer exist -- offered regardless of age, the
  // viewer's own "Connection lost" state covers that case rather than
  // Eden trying to predict retention windows itself.
  const canShowLogs = item.kind === 'script_run' && item.status !== 'rejected';
  return (
    <div className={styles.item}>
      <div className={styles.itemHead}>
        <div className={styles.itemInfo}>
          <div className={styles.itemTop}>
            <KindBadge kind={item.kind} />
            <span className={`${styles.statusBadge} ${finalStatusClass(item)}`}>{finalStatusLabel(item)}</span>
          </div>
          <span className={styles.itemTitle}>{itemTitle(item)}</span>
          <span className={styles.itemMeta}>{itemMeta(item)}</span>
          {argsText && <span className={styles.itemMeta}>{argsText}</span>}
        </div>
        {canShowLogs && (
          <Button size="sm" variant="ghost" onClick={onToggleLogs}>{logsOpen ? 'Hide Logs' : 'Logs'}</Button>
        )}
        {item.mr_url && (
          <a href={item.mr_url} target="_blank" rel="noreferrer" className={styles.mrLink}>View MR ↗</a>
        )}
      </div>
      {logsOpen && <LogViewer runId={item.id} />}
    </div>
  );
}

export function MyRequestsModal({ open, onClose, activeItems, activeLoading, onRefreshActive,
                                   historyItems, historyLoading }) {
  const [expandedId, setExpandedId] = useState(null); // `${kind}:${id}`
  const [editValues, setEditValues] = useState({});
  const [busyKey, setBusyKey] = useState(null);
  // Shared between the active and history lists -- at most one log stream
  // open at a time keeps this simple and avoids piling up EventSource
  // connections if someone clicks "Logs" on several runs in a row.
  const [logsOpenKey, setLogsOpenKey] = useState(null);
  const toggleLogs = (item) => {
    const key = `${item.kind}:${item.id}`;
    setLogsOpenKey(k => (k === key ? null : key));
  };

  const toggleExpand = (item) => {
    const key = `${item.kind}:${item.id}`;
    if (expandedId === key) { setExpandedId(null); return; }
    setExpandedId(key);
    setEditValues(item.kind === 'script_run' ? { ...item.args } : {
      name: item.name, url: item.url, tags: item.tags || [],
    });
  };

  const handleSave = async (item) => {
    const key = `${item.kind}:${item.id}`;
    setBusyKey(key);
    try {
      if (item.kind === 'script_run') {
        await editMyRunArgs(item.id, editValues);
      } else {
        await editMySiteSubmission(item.id, editValues);
      }
      toast.success('Saved');
      setExpandedId(null);
      onRefreshActive?.();
    } catch (e) {
      toast.error(e.message);
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <Modal open={open} onClose={onClose} wide title="My Requests"
           subtitle="Active submissions and runs, with your past ones below"
           footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      {activeLoading ? <p className={styles.loading}>Loading…</p> :
       activeItems.length === 0 ? <p className={styles.empty}>Nothing pending -- you're all caught up.</p> :
       activeItems.map(item => {
        const key = `${item.kind}:${item.id}`;
        return (
          <ActiveItem
            key={key}
            item={item}
            expanded={expandedId === key}
            editValues={editValues}
            busy={busyKey === key}
            onToggleExpand={() => toggleExpand(item)}
            onChange={(name, value) => setEditValues(v => ({ ...v, [name]: value }))}
            onSave={() => handleSave(item)}
            logsOpen={logsOpenKey === key}
            onToggleLogs={() => toggleLogs(item)}
          />
        );
      })}

      <div className={styles.historyHeading}>History</div>
      {historyLoading ? <p className={styles.loading}>Loading…</p> :
       historyItems.length === 0 ? <p className={styles.empty}>Nothing here yet.</p> :
       historyItems.map(item => {
         const key = `${item.kind}:${item.id}`;
         return (
           <HistoryItem key={key} item={item} logsOpen={logsOpenKey === key} onToggleLogs={() => toggleLogs(item)} />
         );
       })}
    </Modal>
  );
}
