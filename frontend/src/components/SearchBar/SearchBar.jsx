import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { fetchDownloads } from '../../api/index.js';
import styles from './SearchBar.module.css';

export function SearchBar({ sites = [], scripts = {} }) {
  const [query, setQuery]   = useState('');
  const [open, setOpen]     = useState(false);
  const [selIdx, setSelIdx] = useState(0);
  const [hidden, setHidden] = useState(false);
  const [downloads, setDownloads] = useState([]);
  const [downloadsLoading, setDownloadsLoading] = useState(false);
  const [downloadsError, setDownloadsError] = useState('');
  const inputRef    = useRef(null);
  const resultsRef  = useRef(null);
  const navigate            = useNavigate();
  const { pathname }        = useLocation();

  // Home screen: full-width bar under the TopBar. Every other screen: a
  // small pill docked in the TopBar itself (between the tabs and the
  // theme toggle) — always at rest in its "in-use" size, not just when
  // focused.
  const docked = pathname !== '/';

  const q = query.toLowerCase().trim();

  const matchesSite = s =>
    s.name?.toLowerCase().includes(q) ||
    s.url?.toLowerCase().includes(q) ||
    (s.tags || []).some(t => t.toLowerCase().includes(q));

  const matchesScript = s =>
    s.name?.toLowerCase().includes(q) ||
    s.description?.toLowerCase().includes(q) ||
    s.team?.toLowerCase().includes(q);

  const filteredSites   = q ? sites.filter(matchesSite) : [];
  const filteredScripts = q ? Object.entries(scripts).flatMap(([team, arr]) =>
    arr.filter(matchesScript).map(s => ({ ...s, team }))
  ) : [];

  // The download catalog can contain thousands of rows. Search it remotely,
  // with a short debounce, only after the user has entered two characters.
  useEffect(() => {
    const search = query.trim();
    if (search.length < 2) {
      setDownloads([]);
      setDownloadsLoading(false);
      setDownloadsError('');
      return undefined;
    }

    const controller = new AbortController();
    setDownloads([]);
    setDownloadsLoading(true);
    setDownloadsError('');
    const timer = window.setTimeout(() => {
      fetchDownloads({ q: search, limit: 8, signal: controller.signal })
        .then(data => setDownloads(Array.isArray(data?.items) ? data.items : []))
        .catch(error => {
          if (error.name !== 'AbortError') {
            setDownloads([]);
            setDownloadsError('Download search is temporarily unavailable');
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setDownloadsLoading(false);
        });
    }, 250);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  // Flat list of all results for keyboard nav
  const allResults = [
    ...filteredSites.map(s   => ({ type: 'site',     data: s })),
    ...downloads.map(d       => ({ type: 'download', data: d })),
    ...filteredScripts.map(s => ({ type: 'script',   data: s })),
  ];

  const hasResults = allResults.length > 0;

  const clear = () => { setQuery(''); setOpen(false); setSelIdx(-1); };

  // Hide SearchBar only when a modal opens — NOT during drag
  useEffect(() => {
    const onModalOpen  = () => setHidden(true);
    const onModalClose = () => setHidden(false);
    window.addEventListener('eden:lockScroll',   onModalOpen);
    window.addEventListener('eden:unlockScroll', onModalClose);
    return () => {
      window.removeEventListener('eden:lockScroll',   onModalOpen);
      window.removeEventListener('eden:unlockScroll', onModalClose);
    };
  }, []);

  // Global "/" key opens search bar
  useEffect(() => {
    const onKeyDown = (e) => {
      // Don't intercept if user is already typing in an input/textarea
      const tag = document.activeElement?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === '/') {
        e.preventDefault();
        inputRef.current?.focus();
        setOpen(true);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  // Close on outside click
  useEffect(() => {
    const h = e => { if (!e.target.closest('[data-searchbar]')) setOpen(false); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, []);

  // Reset selection when results change
  useEffect(() => { setSelIdx(0); }, [q]); // always highlight first result

  // Auto-scroll results list to keep selected item visible
  useEffect(() => {
    if (selIdx < 0 || !resultsRef.current) return;
    const selected = resultsRef.current.querySelector(`.${styles.itemSelected}`);
    if (selected) selected.scrollIntoView({ block: 'nearest' });
  }, [selIdx]);

  // Execute action for a result item
  const openResult = useCallback((item) => {
    if (!item) return;
    if (item.type === 'site') {
      window.location.href = item.data.url;
      clear();
    } else if (item.type === 'script') {
      navigate('/scripts', { state: { openScript: item.data } });
      clear();
    } else if (item.type === 'download') {
      navigate('/downloads', { state: { openDownload: item.data } });
      clear();
    }
  }, [navigate]);

  const handleKeyDown = (e) => {
    if (!open || !hasResults) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelIdx(i => Math.min(i + 1, allResults.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelIdx(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const target = selIdx >= 0 ? allResults[selIdx] : allResults[0];
      openResult(target);
    } else if (e.key === 'Escape') {
      clear();
    }
  };

  // Global result index tracker across groups
  let globalIdx = 0;

  const renderItem = (item, idx) => {
    const isSelected = idx === selIdx;
    const { type, data } = item;

    let img, name, sub, badgeClass, badgeLabel;
    if (type === 'site') {
      img = data.image_url || '/icons/placeholder.svg';
      name = data.name;
      sub = (data.tags || []).join(', '); // never fall back to data.url — don't expose it in search results
      badgeClass = styles.badgeWeb;
      badgeLabel = 'web';
    } else if (type === 'app') {
      img = data.icon_url || '/icons/placeholder.svg';
      name = data.display_name;
      sub = `${data.versions?.length || 0} version(s)`;
      badgeClass = styles.badgeApp;
      badgeLabel = 'install';
    } else if (type === 'script') {
      img = `/api/scripts/${data.team}/${data.folder_name}/logo`;
      name = data.name;
      sub = data.team;
      badgeClass = styles.badgeScript;
      badgeLabel = 'script';
    } else {
      // download
      img = null;
      name = data.display_name || data.name || 'Download';
      sub = `${data.category_label || data.category || ''} · ${data.variant_count || 0} file(s)`;
      badgeClass = styles.badgeDownload;
      badgeLabel = 'download';
    }

    return (
      <div
        key={`${type}-${idx}`}
        className={`${styles.item} ${isSelected ? styles.itemSelected : ''}`}
        onClick={() => openResult(item)}
        onMouseEnter={() => setSelIdx(idx)}
      >
        {img
          ? <img src={img} alt="" className={styles.itemImg}
                 onError={e => { e.target.src = '/icons/placeholder.svg'; }} />
          : <span className={styles.itemImgPlaceholder} aria-hidden="true">↓</span>
        }
        <div className={styles.itemInfo}>
          <span className={styles.itemName}>{name}</span>
          {sub && <span className={styles.itemMeta}>{sub}</span>}
        </div>
        <span className={`${styles.badge} ${badgeClass}`}>{badgeLabel}</span>
        {isSelected && <span className={styles.enterBadge}>↵ enter</span>}
      </div>
    );
  };

  return (
    <div className={`${styles.wrap} ${docked ? styles.docked : ''}`} data-searchbar
         style={hidden ? { opacity: 0, pointerEvents: 'none' } : {}}>
      <div className={styles.inner}>
        <span className={styles.icon}>⌕</span>
        <input
          ref={inputRef}
          className={styles.input}
          type="text"
          value={query}
          placeholder={docked ? 'Search… (/)' : 'Search apps, websites, scripts, tags… (press / to focus)'}
          onChange={e => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          autoComplete="off"
        />
        {query && (
          <button type="button" className={styles.clearBtn} onClick={clear}>✕</button>
        )}

        {/* Nested inside .inner (not a sibling) so its position:absolute
            resolves against the actual pill — which has real, correct
            dimensions — rather than against .wrap, which is just a
            full-viewport click-through shell and would push this to
            "100% of the viewport height", off-screen. */}
        {open && q && (
        <div className={`${styles.results} ${docked ? styles.dockedResults : ''}`} ref={resultsRef}>
          {!hasResults && downloadsLoading && (
            <div className={styles.noResults}>Searching downloads…</div>
          )}

          {!hasResults && !downloadsLoading && q.length < 2 && (
            <div className={styles.noResults}>Type at least 2 characters to search downloads</div>
          )}

          {!hasResults && !downloadsLoading && downloadsError && (
            <div className={styles.noResults}>{downloadsError}</div>
          )}

          {!hasResults && !downloadsLoading && !downloadsError && q.length >= 2 && (
            <div className={styles.noResults}>No results for "{query}"</div>
          )}

          {filteredSites.length > 0 && (
            <>
              <div className={styles.groupLabel}>Web Apps</div>
              {filteredSites.map((s, i) => {
                const idx = globalIdx++;
                return renderItem({ type: 'site', data: s }, idx);
              })}
            </>
          )}

          {downloads.length > 0 && (
            <>
              <div className={styles.groupLabel}>Downloads</div>
              {downloads.map((d, i) => {
                const idx = globalIdx++;
                return renderItem({ type: 'download', data: d }, idx);
              })}
            </>
          )}

          {filteredScripts.length > 0 && (
            <>
              <div className={styles.groupLabel}>Scripts</div>
              {filteredScripts.map((s, i) => {
                const idx = globalIdx++;
                return renderItem({ type: 'script', data: s }, idx);
              })}
            </>
          )}

          {hasResults && (
            <div className={styles.hint}>
              ↑↓ navigate · ↵ open · esc close
            </div>
          )}
        </div>
        )}
      </div>
    </div>
  );
}
