import React, { useEffect, useMemo, useState } from 'react';
import toast from 'react-hot-toast';
import { fetchDownloads, uploadDownload } from '../../api/index.js';
import { Button } from '../Button/Button.jsx';
import { Modal } from '../Modal/Modal.jsx';
import styles from './DownloadUploadModal.module.css';

const INITIAL_FORM = {
  name: '',
  description: '',
  category: 'development',
  publisher: '',
  tags: '',
  aliases: '',
  member_name: '',
  version: '',
  architecture: 'x64',
  operating_system: 'Windows',
  file_type: '',
};

function itemId(item) {
  return item?.catalog_id ?? item?.id;
}

function itemName(item) {
  return item?.display_name || item?.name || 'Untitled download';
}

function commaList(value) {
  return value
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function inferredFileType(file) {
  const name = file?.name || '';
  const separator = name.lastIndexOf('.');
  return separator > -1 ? name.slice(separator + 1).toLowerCase() : '';
}

export function DownloadUploadModal({
  open,
  mode,
  categories,
  onClose,
  onUploaded,
}) {
  const isNewApp = mode === 'new';
  const [form, setForm] = useState(INITIAL_FORM);
  const [artifact, setArtifact] = useState(null);
  const [targetQuery, setTargetQuery] = useState('');
  const [targetResults, setTargetResults] = useState([]);
  const [selectedTarget, setSelectedTarget] = useState(null);
  const [searchStatus, setSearchStatus] = useState('idle');
  const [searchError, setSearchError] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  // Bumped on every reset so the artifact <input type="file"> remounts —
  // clearing `artifact` state alone doesn't clear what the browser displays
  // for a native file input, since it's an uncontrolled element.
  const [resetKey, setResetKey] = useState(0);

  useEffect(() => {
    if (!open) return;
    setForm({
      ...INITIAL_FORM,
      category: categories?.[0]?.slug || INITIAL_FORM.category,
    });
    setArtifact(null);
    setTargetQuery('');
    setTargetResults([]);
    setSelectedTarget(null);
    setSearchStatus('idle');
    setSearchError('');
    setUploading(false);
    setUploadError('');
    setResetKey(k => k + 1);
  }, [mode, open]);

  useEffect(() => {
    if (!open || isNewApp || selectedTarget) return undefined;
    const query = targetQuery.trim();
    if (query.length < 2) {
      setTargetResults([]);
      setSearchStatus('idle');
      setSearchError('');
      return undefined;
    }

    const controller = new AbortController();
    setSearchStatus('loading');
    setSearchError('');
    const timer = window.setTimeout(() => {
      fetchDownloads({ q: query, limit: 8, signal: controller.signal })
        .then((data) => {
          setTargetResults(Array.isArray(data?.items) ? data.items : []);
          setSearchStatus('ready');
        })
        .catch((error) => {
          if (error.name === 'AbortError') return;
          setTargetResults([]);
          setSearchStatus('error');
          setSearchError(error.message || 'Apps could not be searched.');
        });
    }, 250);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [isNewApp, open, selectedTarget, targetQuery]);

  const title = isNewApp ? 'Upload New App' : 'Add App Version';
  const submitLabel = isNewApp ? 'Upload new app' : 'Add version';
  const selectedTargetId = itemId(selectedTarget);
  const canSubmit = Boolean(
    artifact
    && form.version.trim()
    && form.architecture.trim()
    && (isNewApp ? form.name.trim() && form.category : selectedTargetId),
  );

  const metadata = useMemo(() => {
    const variant = {
      member_name: form.member_name.trim(),
      version: form.version.trim(),
      architecture: form.architecture.trim(),
      operating_system: form.operating_system.trim(),
      file_type: form.file_type.trim().toLowerCase(),
    };
    if (!isNewApp) {
      return { catalog_id: selectedTargetId, ...variant };
    }
    return {
      name: form.name.trim(),
      description: form.description.trim(),
      category: form.category,
      publisher: form.publisher.trim(),
      tags: commaList(form.tags),
      aliases: commaList(form.aliases),
      ...variant,
    };
  }, [form, isNewApp, selectedTargetId]);

  const updateField = (field) => (event) => {
    setForm((current) => ({ ...current, [field]: event.target.value }));
    setUploadError('');
  };

  const handleArtifact = (event) => {
    const file = event.target.files?.[0] || null;
    setArtifact(file);
    setUploadError('');
    if (file) {
      setForm((current) => ({
        ...current,
        file_type: inferredFileType(file) || current.file_type,
      }));
    }
  };

  const handleSubmit = async () => {
    if (!canSubmit) {
      setUploadError(
        isNewApp
          ? 'Name, category, version, architecture, and artifact are required.'
          : 'Choose an app and provide a version, architecture, and artifact.',
      );
      return;
    }

    setUploading(true);
    setUploadError('');
    try {
      const body = new FormData();
      body.append('mode', mode);
      body.append('metadata', JSON.stringify(metadata));
      Object.entries(metadata).forEach(([key, value]) => {
        body.append(key, Array.isArray(value) ? value.join(',') : String(value ?? ''));
      });
      body.append('artifact', artifact, artifact.name);
      const result = await uploadDownload(body);
      toast.success(isNewApp ? 'New app uploaded' : `Version added to ${itemName(selectedTarget)}`);
      await onUploaded?.(result, metadata);
      onClose();
    } catch (error) {
      setUploadError(error.message || 'The artifact could not be uploaded.');
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      subtitle={isNewApp
        ? 'Create one catalog app and attach its first downloadable file'
        : 'Attach another version or architecture to an existing app'}
      wide
      confirmLeave
      footer={(
        <>
          <Button variant="secondary" onClick={onClose} disabled={uploading}>Cancel</Button>
          <Button loading={uploading} disabled={!canSubmit} onClick={handleSubmit}>
            {submitLabel}
          </Button>
        </>
      )}
    >
      <div className={styles.form}>
        {!isNewApp && (
          <section className={styles.targetSection} aria-labelledby="download-upload-target">
            <span id="download-upload-target" className={styles.fieldLabel}>Existing app *</span>
            {selectedTarget ? (
              <div className={styles.selectedTarget}>
                <div>
                  <strong>{itemName(selectedTarget)}</strong>
                  <span>
                    {selectedTarget.category_label || selectedTarget.category || 'Download'}
                    {' · '}
                    {selectedTarget.variant_count || 0} existing file(s)
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedTarget(null);
                    setTargetQuery('');
                    setTargetResults([]);
                  }}
                >
                  Change
                </button>
              </div>
            ) : (
              <div className={styles.targetSearch}>
                <input
                  className={styles.input}
                  type="search"
                  value={targetQuery}
                  placeholder="Search apps (2+ characters)"
                  autoComplete="off"
                  onChange={(event) => setTargetQuery(event.target.value)}
                />
                <div className={styles.searchMessage} aria-live="polite">
                  {targetQuery.trim().length < 2 && 'Type at least 2 characters. The full catalog is never preloaded.'}
                  {searchStatus === 'loading' && 'Searching catalog…'}
                  {searchStatus === 'error' && searchError}
                  {searchStatus === 'ready' && targetResults.length === 0 && 'No matching apps.'}
                </div>
                {targetResults.length > 0 && (
                  <div className={styles.targetResults} role="listbox" aria-label="Matching apps">
                    {targetResults.map((item) => (
                      <button
                        key={itemId(item)}
                        type="button"
                        role="option"
                        aria-selected="false"
                        onClick={() => {
                          setSelectedTarget(item);
                          setTargetResults([]);
                        }}
                      >
                        <strong>{itemName(item)}</strong>
                        <span>
                          {item.category_label || item.category || 'Download'}
                          {' · '}
                          {item.variant_count || 0} file(s)
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </section>
        )}

        {isNewApp && (
          <>
            <div className={styles.twoColumns}>
              <FormField label="App name *">
                <input
                  className={styles.input}
                  value={form.name}
                  placeholder="Visual Studio Code"
                  onChange={updateField('name')}
                />
              </FormField>
              <FormField label="Category *">
                <select className={styles.input} value={form.category} onChange={updateField('category')}>
                  {categories.map((category) => (
                    <option key={category.slug} value={category.slug}>{category.label}</option>
                  ))}
                </select>
              </FormField>
            </div>

            <FormField label="Description">
              <textarea
                className={`${styles.input} ${styles.textarea}`}
                value={form.description}
                placeholder="What this app is used for"
                rows="3"
                onChange={updateField('description')}
              />
            </FormField>

            <div className={styles.twoColumns}>
              <FormField label="Publisher">
                <input
                  className={styles.input}
                  value={form.publisher}
                  placeholder="Microsoft"
                  onChange={updateField('publisher')}
                />
              </FormField>
              <FormField label="Search tags">
                <input
                  className={styles.input}
                  value={form.tags}
                  placeholder="editor, development, code"
                  onChange={updateField('tags')}
                />
                <span className={styles.fieldHint}>Comma-separated</span>
              </FormField>
            </div>

            <FormField label="Alternative names">
              <input
                className={styles.input}
                value={form.aliases}
                placeholder="VS Code, vscode"
                onChange={updateField('aliases')}
              />
              <span className={styles.fieldHint}>Comma-separated names that should also match search</span>
            </FormField>
          </>
        )}

        <div className={styles.divider}>
          <span>{isNewApp ? 'First downloadable file' : 'New downloadable file'}</span>
        </div>

        <FormField label="Product/component">
          <input
            className={styles.input}
            value={form.member_name}
            placeholder="Optional, e.g. Ultimate or Team Foundation Server"
            onChange={updateField('member_name')}
          />
          <span className={styles.fieldHint}>Groups related products under the same app.</span>
        </FormField>

        <div className={styles.threeColumns}>
          <FormField label="Version *">
            <input
              className={styles.input}
              value={form.version}
              placeholder="1.96.4"
              onChange={updateField('version')}
            />
          </FormField>
          <FormField label="Architecture *">
            <input
              className={styles.input}
              list="download-architecture-options"
              value={form.architecture}
              placeholder="x64"
              onChange={updateField('architecture')}
            />
            <datalist id="download-architecture-options">
              <option value="x64" />
              <option value="x86" />
              <option value="arm64" />
              <option value="universal" />
            </datalist>
          </FormField>
          <FormField label="Platform / OS">
            <input
              className={styles.input}
              list="download-os-options"
              value={form.operating_system}
              placeholder="Windows"
              onChange={updateField('operating_system')}
            />
            <datalist id="download-os-options">
              <option value="Windows" />
              <option value="Linux" />
              <option value="macOS" />
              <option value="Cross-platform" />
            </datalist>
          </FormField>
        </div>

        <div className={styles.twoColumns}>
          <FormField label="Artifact file *">
            <input key={`artifact-${resetKey}`} className={`${styles.input} ${styles.fileInput}`} type="file" onChange={handleArtifact} />
            <span className={styles.fieldHint}>The file is uploaded directly to object storage.</span>
          </FormField>
          <FormField label="File format">
            <input
              className={styles.input}
              value={form.file_type}
              placeholder="Automatically read from filename"
              onChange={updateField('file_type')}
            />
          </FormField>
        </div>

        {artifact && (
          <div className={styles.fileSummary}>
            <span aria-hidden="true">↑</span>
            <div>
              <strong>{artifact.name}</strong>
              <small>{formatFileSize(artifact.size)}</small>
            </div>
          </div>
        )}

        {uploadError && <p className={styles.error} role="alert">{uploadError}</p>}
      </div>
    </Modal>
  );
}

function FormField({ label, children }) {
  return (
    <label className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      {children}
    </label>
  );
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${bytes || 0} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}
