import React, { useState, useCallback, useEffect } from 'react';
import toast from 'react-hot-toast';
import {
  DndContext, closestCenter, PointerSensor, useSensor, useSensors,
} from '@dnd-kit/core';
import { restrictToParentElement } from '@dnd-kit/modifiers';
import {
  SortableContext, rectSortingStrategy, useSortable, arrayMove,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { SiteCard }   from '../../components/SiteCard/SiteCard.jsx';
import { Modal }      from '../../components/Modal/Modal.jsx';
import { Button }     from '../../components/Button/Button.jsx';
import {
  submitSite, fetchPendingSites, reviewSite,
  deleteSite, editSite, fetchSites, fetchBannerOptions,
  fetchEnvColorOptions,
  fetchUsers, setUserAdmin,
} from '../../api/index.js';
import styles from './WebAppsPage.module.css';

// ── Sortable wrapper for starred cards ────────────────────────────────────────
function SortableCard({ site, isStarred, onToggleStar, isAdmin, onEdit, onDelete, onRefEl }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: site.id });
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.4 : 1 };

  const setRefs = (el) => {
    setNodeRef(el);
    if (onRefEl) onRefEl(el);
  };

  return (
    <div ref={setRefs} style={style} {...attributes} {...listeners}>
      <SiteCard site={site} isStarred={isStarred} onToggleStar={onToggleStar}
                isAdmin={isAdmin} onEdit={onEdit} onDelete={onDelete} />
    </div>
  );
}

// ── Tag colour helper ─────────────────────────────────────────────────────────
const COLOURS = ['#4fffb0','#00c9ff','#f59e0b','#a78bfa','#f472b6','#34d399','#fb923c'];
const tagColor = tag => COLOURS[[...tag].reduce((a,c) => a + c.charCodeAt(0), 0) % COLOURS.length];

export function WebAppsPage({ sites, setSites, starredSites, starredIds, onToggleStar, onReorder, onSitesChanged, isAdmin, cardRefs }) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }));

  // ── Fetch banner / env-color options ────────────────────────────────────
  useEffect(() => {
    fetchBannerOptions().then(d => setBannerOptions(Array.isArray(d) ? d : [])).catch(() => {});
    fetchEnvColorOptions().then(d => setEnvColorOptions(Array.isArray(d) ? d : [])).catch(() => {});
  }, []);

  // ── Submit modal state ────────────────────────────────────────────────────
  const [submitOpen,    setSubmitOpen]    = useState(false);
  const [submitForm,    setSubmitForm]    = useState({ name:'', url:'', favicon_url:'', tags:'', group_name:'', group_display_name:'', env_label:'', env_color:'' });
  const [bannerOptions, setBannerOptions] = useState([]);
  const [envColorOptions, setEnvColorOptions] = useState([]);
  const [submitFile,    setSubmitFile]    = useState(null);
  const [submitLoading, setSubmitLoading] = useState(false);

  // ── Edit modal state ──────────────────────────────────────────────────────
  const [editOpen,    setEditOpen]    = useState(false);
  const [editSiteObj, setEditSiteObj] = useState(null);
  const [editForm,    setEditForm]    = useState({ name:'', url:'', tags:'', favicon_url:'', group_name:'', group_display_name:'', env_label:'', env_color:'' });
  const [editFile,    setEditFile]    = useState(null);
  const [editLoading, setEditLoading] = useState(false);

  // ── Pending modal state ───────────────────────────────────────────────────
  const [pendingOpen,  setPendingOpen]  = useState(false);
  const [pendingList,  setPendingList]  = useState([]);
  const [pendingLoading, setPendingLoading] = useState(false);

  // ── Users modal state ────────────────────────────────────────────────────
  const [usersOpen,   setUsersOpen]   = useState(false);
  const [usersList,   setUsersList]   = useState([]);
  const [usersLoading, setUsersLoading] = useState(false);

  const openUsers = async () => {
    setUsersOpen(true); setUsersLoading(true);
    try { setUsersList(await fetchUsers()); }
    catch { toast.error('Failed to load users'); }
    finally { setUsersLoading(false); }
  };

  const handleSetAdmin = async (username, makeAdmin) => {
    try {
      await setUserAdmin(username, makeAdmin);
      toast.success(makeAdmin ? `${username} is now admin` : `${username} is no longer admin`);
      setUsersList(prev => prev.map(u => u.username === username ? { ...u, is_admin: makeAdmin } : u));
    } catch (e) { toast.error(e.message); }
  };

  // ── Delete confirm ────────────────────────────────────────────────────────
  const [deleteTarget, setDeleteTarget] = useState(null);

  // ── Drag to reorder starred ───────────────────────────────────────────────
  const lockScroll   = () => window.dispatchEvent(new CustomEvent('eden:dragStart'));
  const unlockScroll = () => window.dispatchEvent(new CustomEvent('eden:dragEnd'));

  const handleDragEnd = ({ active, over }) => {
    unlockScroll();
    // Suppress click that fires after drag by catching it at capture phase
    const suppressNextClick = (e) => { e.stopPropagation(); e.preventDefault(); };
    window.addEventListener('click', suppressNextClick, { capture: true, once: true });
    setTimeout(() => window.removeEventListener('click', suppressNextClick, true), 300);
    if (!over || active.id === over.id) return;
    const oi = starredSites.findIndex(s => s.id === active.id);
    const ni = starredSites.findIndex(s => s.id === over.id);
    onReorder(arrayMove(starredSites, oi, ni));
  };

  // ── Submit handlers ───────────────────────────────────────────────────────
  const openSubmit = () => {
    setSubmitForm({ name:'', url:'', favicon_url:'', tags:'', group_name:'', group_display_name:'', env_label:'', env_color:'' });
    setSubmitFile(null);
    setSubmitOpen(true);
  };

  const handleSubmit = async () => {
    if (!submitForm.name || !submitForm.url) { toast.error('Name and URL required'); return; }
    if (!submitForm.url.startsWith('http')) { toast.error('URL must start with http:// or https://'); return; }
    setSubmitLoading(true);
    try {
      let favicon_url = submitForm.favicon_url;
      if (submitFile) {
        favicon_url = await fileToDataUrl(submitFile);
      }
      await submitSite({
        ...submitForm,
        favicon_url,
        tags:               parseTags(submitForm.tags),
        group_name:         (submitForm.group_name || '').trim() || null,
        group_display_name: (submitForm.group_display_name || '').trim() || null,
        env_label:          (submitForm.env_label || '').trim() || null,
        env_color:          submitForm.env_color || null,
      });
      toast.success('Submitted for approval!');
      setSubmitOpen(false);
    } catch (e) { toast.error(e.message); }
    finally { setSubmitLoading(false); }
  };

  // ── Edit handlers ─────────────────────────────────────────────────────────
  const openEdit = (site) => {
    setEditSiteObj(site);
    setEditForm({
      name: site.name, url: site.url,
      tags: (site.tags||[]).join(', '), favicon_url: '',
      group_name: site.group_name || '',
      group_display_name: site.group_display_name || '',
      env_label: site.env_label || '',
      env_color: site.env_color || '',
    });
    setEditFile(null);
    setEditOpen(true);
  };

  const handleEdit = async () => {
    if (!editForm.name || !editForm.url) { toast.error('Name and URL required'); return; }
    setEditLoading(true);
    try {
      let favicon_url = editForm.favicon_url || null;
      let favicon_data = null;
      if (editFile) favicon_data = await fileToDataUrl(editFile);
      await editSite({
        site_id:            editSiteObj.id,
        name:               editForm.name,
        url:                editForm.url,
        tags:               parseTags(editForm.tags),
        favicon_url,
        favicon_data,
        group_name:         (editForm.group_name || '').trim() || null,
        group_display_name: (editForm.group_display_name || '').trim() || null,
        env_label:          (editForm.env_label || '').trim() || null,
        env_color:          editForm.env_color || null,
      });
      toast.success('Site updated');
      setEditOpen(false);
      const fresh = await fetchSites();
      setSites(fresh);
      // starredSites (what Home's GroupedSiteCard actually renders from) is
      // separate state owned by useStars() -- refreshing `sites` here never
      // touched it, so an edited starred site kept showing its old env_color
      // (or group/label/etc.) in the expanded card, and reopening Edit on it
      // read that same stale object straight back into the form.
      onSitesChanged?.();
    } catch (e) { toast.error(e.message); }
    finally { setEditLoading(false); }
  };

  // ── Delete handlers ───────────────────────────────────────────────────────
  const openDelete  = (site) => setDeleteTarget(site);
  const handleDelete = async () => {
    try {
      await deleteSite(deleteTarget.id);
      setSites(prev => prev.filter(s => s.id !== deleteTarget.id));
      onSitesChanged?.(); // same staleness issue as handleEdit -- deleting a
                          // starred site would otherwise leave a ghost card on Home
      toast.success(`Deleted: ${deleteTarget.name}`);
    } catch (e) { toast.error(e.message); }
    finally { setDeleteTarget(null); }
  };

  // ── Pending handlers ──────────────────────────────────────────────────────
  const openPending = async () => {
    setPendingOpen(true);
    setPendingLoading(true);
    try { setPendingList(await fetchPendingSites()); }
    catch { toast.error('Failed to load pending'); }
    finally { setPendingLoading(false); }
  };

  const handleReview = async (id, approve) => {
    try {
      await reviewSite(id, approve);
      toast.success(approve ? 'Approved!' : 'Rejected');
      setPendingList(prev => prev.filter(s => s.id !== id));
      if (approve) { const fresh = await fetchSites(); setSites(fresh); }
    } catch (e) { toast.error(e.message); }
  };

  const otherSites = sites.filter(s => !starredIds.has(s.id));

  return (
    <div className={styles.page}>
      {/* ── Starred section ── */}
      {starredSites.length > 0 && (
        <section className={styles.section}>
          <div className={styles.sectionHeader}>
            <span className={styles.sectionTitle}>⭐ Starred</span>
            <span className={styles.hint}>Drag to reorder</span>
          </div>
          <DndContext sensors={sensors} collisionDetection={closestCenter}
                      modifiers={[restrictToParentElement]}
                      onDragStart={lockScroll} onDragEnd={handleDragEnd} onDragCancel={unlockScroll}>
            <SortableContext items={starredSites.map(s => s.id)} strategy={rectSortingStrategy}>
              <div className={styles.grid}>
                {starredSites.map(site => (
                  <SortableCard key={site.id} site={site} isStarred={true}
                                onToggleStar={onToggleStar} isAdmin={isAdmin}
                                onEdit={openEdit} onDelete={openDelete}
                                onRefEl={(el) => { if (cardRefs?.current) cardRefs.current[site.id] = el; }} />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        </section>
      )}

      {/* ── All apps section ── */}
      <section className={styles.section}>
        {starredSites.length > 0 && (
          <div className={styles.sectionHeader}>
            <span className={styles.sectionTitle}>All Apps</span>
          </div>
        )}
        <div className={styles.grid}>
          {otherSites.map(site => (
            <SiteCard key={site.id} site={site}
                      isStarred={starredIds.has(site.id)}
                      onToggleStar={onToggleStar}
                      isAdmin={isAdmin}
                      onEdit={openEdit}
                      onDelete={openDelete} />
          ))}
          {otherSites.length === 0 && !starredSites.length && (
            <div className={styles.empty}>No web apps yet — submit the first one!</div>
          )}
        </div>
      </section>

      {/* ── Action buttons ── */}
      <div className={styles.actions}>
        <Button variant="ghost" onClick={openSubmit}>+ Submit New Web App</Button>
        {isAdmin && (
          <>
            <Button variant="ghost" onClick={openPending}>⏳ Review Pending</Button>
            <Button variant="ghost" onClick={openUsers}>👤 Manage Users</Button>
          </>
        )}
      </div>

      {/* ── Submit modal ── */}
      <Modal open={submitOpen} onClose={() => setSubmitOpen(false)}
             title="Submit New Web App" subtitle="Sent for approval before appearing for everyone"
             confirmLeave
             footer={
               <>
                 <Button variant="secondary" onClick={() => setSubmitOpen(false)}>Cancel</Button>
                 <Button loading={submitLoading} onClick={handleSubmit}>Submit for Approval</Button>
               </>
             }>
        <SubmitSiteForm form={submitForm} setForm={setSubmitForm}
                        file={submitFile} setFile={setSubmitFile}
                        bannerOptions={bannerOptions} envColorOptions={envColorOptions} />
      </Modal>

      {/* ── Edit modal ── */}
      <Modal open={editOpen} onClose={() => setEditOpen(false)}
             title="Edit Web App" subtitle={editSiteObj?.url}
             confirmLeave
             footer={
               <>
                 <Button variant="secondary" onClick={() => setEditOpen(false)}>Cancel</Button>
                 <Button loading={editLoading} onClick={handleEdit}>Save Changes</Button>
               </>
             }>
        <EditSiteForm form={editForm} setForm={setEditForm}
                      file={editFile} setFile={setEditFile}
                      bannerOptions={bannerOptions} envColorOptions={envColorOptions} />
      </Modal>

      {/* ── Pending modal ── */}
      <Modal open={pendingOpen} onClose={() => setPendingOpen(false)} title="Pending Submissions" wide
             footer={<Button variant="secondary" onClick={() => setPendingOpen(false)}>Close</Button>}>
        {pendingLoading ? <div className={styles.loading}>Loading…</div> :
         pendingList.length === 0 ? <div className={styles.empty}>No pending submissions</div> :
         pendingList.map(item => (
           <div key={item.id} className={styles.pendingItem}>
             <img src={item.favicon_url || '/icons/placeholder.svg'} alt=""
                  onError={e => { e.target.src='/icons/placeholder.svg'; }} />
             <div className={styles.pendingInfo}>
               <span className={styles.pendingName}>{item.name}</span>
               <span className={styles.pendingMeta}>{item.url} · by {item.submitted_by}</span>
             </div>
             <div className={styles.pendingActions}>
               <Button size="sm" onClick={() => handleReview(item.id, true)}>Approve</Button>
               <Button size="sm" variant="danger" onClick={() => handleReview(item.id, false)}>Reject</Button>
             </div>
           </div>
         ))
        }
      </Modal>

      {/* ── Manage users modal ── */}
      <Modal open={usersOpen} onClose={() => setUsersOpen(false)} wide title="Manage Users"
             subtitle="Promote or demote admin access"
             footer={<Button variant="secondary" onClick={() => setUsersOpen(false)}>Close</Button>}>
        {usersLoading ? <div className={styles.loading}>Loading…</div> :
         usersList.length === 0 ? <div className={styles.empty}>No users yet</div> :
         usersList.map(u => (
           <div key={u.username} className={styles.pendingItem}>
             <div className={styles.pendingInfo}>
               <span className={styles.pendingName}>
                 {u.username}
                 {u.is_admin && <span style={{marginLeft:8,fontSize:'0.6rem',color:'var(--accent)',fontFamily:'var(--font-mono)'}}>admin</span>}
               </span>
               <span className={styles.pendingMeta}>
                 First seen: {new Date(u.first_seen).toLocaleDateString()} ·
                 Last seen: {new Date(u.last_seen).toLocaleDateString()}
               </span>
             </div>
             <div className={styles.pendingActions}>
               {u.is_admin
                 ? <Button size="sm" variant="danger" onClick={() => handleSetAdmin(u.username, false)}>Remove Admin</Button>
                 : <Button size="sm" onClick={() => handleSetAdmin(u.username, true)}>Make Admin</Button>
               }
             </div>
           </div>
         ))
        }
      </Modal>

      {/* ── Delete confirm modal ── */}
      <Modal open={!!deleteTarget} onClose={() => setDeleteTarget(null)}
             title="Delete Web App"
             footer={
               <>
                 <Button variant="secondary" onClick={() => setDeleteTarget(null)}>Cancel</Button>
                 <Button variant="danger" onClick={handleDelete}>Delete</Button>
               </>
             }>
        <p className={styles.deleteMsg}>
          Are you sure you want to delete <strong>{deleteTarget?.name}</strong>?<br/>
          This cannot be undone.
        </p>
      </Modal>
    </div>
  );
}

// ── Sub-forms ─────────────────────────────────────────────────────────────────
function BannerSelect({ value, onChange, options, className }) {
  return (
    <select className={className} value={value} onChange={e => onChange(e.target.value)}>
      {options.map(o => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

// Color for a GroupedSiteCard's environment row: bullet + frame. Native
// <select> can't render a colored swatch per <option> reliably across
// browsers, so the color shows via a dot on the trigger itself instead.
function EnvColorSelect({ value, onChange, options, className }) {
  const current = options.find(o => o.value === value);
  return (
    <div className={styles.envColorRow}>
      {current?.color && (
        <span className={styles.envColorDot} style={{ background: current.color }} />
      )}
      <select className={className} value={value || ''} onChange={e => onChange(e.target.value)}>
        {options.map(o => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

function SubmitSiteForm({ form, setForm, file, setFile, bannerOptions, envColorOptions }) {
  const f = (k) => (e) => setForm(p => ({ ...p, [k]: e.target.value }));
  return (
    <div className={styles.form}>
      <FormGroup label="Name *">
        <input className={styles.input} value={form.name} onChange={f('name')} placeholder="Grafana" />
      </FormGroup>
      <FormGroup label="URL *">
        <input className={styles.input} value={form.url} onChange={f('url')} placeholder="http://grafana.internal" />
      </FormGroup>
      <FormGroup label="Logo — upload file">
        <input className={styles.input} type="file" accept=".png,.jpg,.jpeg"
               onChange={e => { setFile(e.target.files[0]); setForm(p=>({...p,favicon_url:''})); }} />
      </FormGroup>
      <FormGroup label="Banner">
        <BannerSelect className={styles.input} value={form.tags || ''} options={bannerOptions}
                      onChange={v => setForm(p => ({ ...p, tags: v }))} />
      </FormGroup>
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Group name
          <span className={styles.infoIcon} title="Apps sharing a group name stack into one card on the home screen. Leave blank for standalone.">i</span>
        </span>
      }>
        <input className={styles.input} value={form.group_name || ''} onChange={f('group_name')}
               placeholder="e.g. argocd" />
      </FormGroup>
      <FormGroup label="Group display name">
        <input className={styles.input} value={form.group_display_name || ''} onChange={f('group_display_name')}
               placeholder="e.g. ArgoCD (leave blank to auto-capitalize)" />
      </FormGroup>
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Environment Label
          <span className={styles.infoIcon} title="Shown in the environment picker when the card is expanded.">i</span>
        </span>
      }>
        <input className={styles.input} value={form.env_label || ''} onChange={f('env_label')}
               placeholder="e.g. Production, Staging, DR Cluster" />
      </FormGroup>
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Environment Color
          <span className={styles.infoIcon} title="Bullet + frame color for this environment's row when the grouped card is expanded.">i</span>
        </span>
      }>
        <EnvColorSelect className={styles.input} value={form.env_color || ''} options={envColorOptions}
                        onChange={v => setForm(p => ({ ...p, env_color: v }))} />
      </FormGroup>
    </div>
  );
}


function EditSiteForm({ form, setForm, file, setFile, bannerOptions, envColorOptions }) {
  const f = (k) => (e) => setForm(p => ({ ...p, [k]: e.target.value }));
  return (
    <div className={styles.form}>
      <FormGroup label="Name">
        <input className={styles.input} value={form.name} onChange={f('name')} />
      </FormGroup>
      <FormGroup label="URL">
        <input className={styles.input} value={form.url} onChange={f('url')} />
      </FormGroup>
      <FormGroup label="Banner">
        <BannerSelect className={styles.input} value={form.tags || ''} options={bannerOptions}
                      onChange={v => setForm(p => ({ ...p, tags: v }))} />
      </FormGroup>
      <FormGroup label="New Logo — upload file (leave blank to keep existing)">
        <input className={styles.input} type="file" accept=".png,.jpg,.jpeg"
               onChange={e => { setFile(e.target.files[0]); setForm(p=>({...p,favicon_url:''})); }} />
      </FormGroup>

      {/* ── Group / environment ── */}
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Group name
          <span className={styles.infoIcon} title="Apps sharing a group name stack into one card on the home screen. Leave blank for standalone.">i</span>
        </span>
      }>
        <input className={styles.input} value={form.group_name} onChange={f('group_name')}
               placeholder="e.g. argocd" />
        {/* <span className={styles.hint}>Apps sharing a group name stack into one card on the home screen. Leave blank for standalone.</span> */}
      </FormGroup>
      <FormGroup label="Group display name">
        <input className={styles.input} value={form.group_display_name} onChange={f('group_display_name')}
               placeholder="e.g. ArgoCD (leave blank to auto-capitalize)" />
      </FormGroup>
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Environment Label
          <span className={styles.infoIcon} title="Shown in the environment picker when the card is expanded.">i</span>
        </span>
      }>
        <input className={styles.input} value={form.env_label || ''} onChange={f('env_label')}
               placeholder="e.g. Production, Staging, DR Cluster" />
      </FormGroup>
      <FormGroup label={
        <span style={{display:'flex',alignItems:'center',gap:6}}>
          Environment Color
          <span className={styles.infoIcon} title="Bullet + frame color for this environment's row when the grouped card is expanded.">i</span>
        </span>
      }>
        <EnvColorSelect className={styles.input} value={form.env_color || ''} options={envColorOptions}
                        onChange={v => setForm(p => ({ ...p, env_color: v }))} />
      </FormGroup>
    </div>
  );
}

function FormGroup({ label, children }) {
  return (
    <div className={styles.formGroup}>
      <label className={styles.label}>{label}</label>
      {children}
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const parseTags = (s) => s ? [s.trim()].filter(Boolean) : [];
const fileToDataUrl = (file) => new Promise((res, rej) => {
  const r = new FileReader();
  r.onload = () => res(r.result);
  r.onerror = rej;
  r.readAsDataURL(file);
});
