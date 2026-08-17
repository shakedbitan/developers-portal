import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useLocation } from 'react-router-dom';
import { Modal } from '../../components/Modal/Modal.jsx';
import { Button } from '../../components/Button/Button.jsx';
import { DownloadCard } from '../../components/DownloadCard/DownloadCard.jsx';
import { DownloadUploadModal } from '../../components/DownloadUploadModal/DownloadUploadModal.jsx';
import styles from './DownloadsPage.module.css';

const PAGE_SIZE = 48;
const SEARCH_DELAY_MS = 300;

const DEFAULT_CATEGORIES = [
  {
    slug: 'development',
    label: 'Development',
    icon: 'DEV',
    description: 'Editors, runtimes, SDKs, source bundles, and developer packages.',
  },
  {
    slug: 'infrastructure',
    label: 'IT & Infrastructure',
    icon: 'OPS',
    description: 'Remote access, identity, networking, monitoring, and admin utilities.',
  },
  {
    slug: 'security',
    label: 'Security',
    icon: 'SEC',
    description: 'Endpoint protection, scanning tools, and security agents.',
  },
  {
    slug: 'data',
    label: 'Data & Databases',
    icon: 'DB',
    description: 'Datasets, database assets, exports, and offline data packages.',
  },
  {
    slug: 'productivity',
    label: 'Productivity & Design',
    icon: 'UX',
    description: 'Office, graphics, drawing, diagramming, media, and design software.',
  },
  {
    slug: 'utilities',
    label: 'Utilities',
    icon: 'UTIL',
    description: 'General-purpose tools, compression, cleanup, and system utilities.',
  },
  {
    slug: 'drivers',
    label: 'Drivers & Firmware',
    icon: 'DRV',
    description: 'Device drivers, firmware images, and hardware support packages.',
  },
  {
    slug: 'other',
    label: 'Other',
    icon: 'ETC',
    description: 'Specialist, uncategorized, and miscellaneous offline packages.',
  },
];

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      Accept: 'application/json',
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || data.message || `Request failed (${response.status})`);
  }
  return data;
}

function catalogId(item) {
  return item?.catalog_id ?? item?.id;
}

function variantId(variant) {
  return variant?.variant_id ?? variant?.id;
}

function variantArchitecture(variant) {
  return variant?.architecture || variant?.arch || 'Universal';
}

function variantVersion(variant) {
  return variant?.version || variant?.version_label || 'Latest';
}

function variantMember(variant) {
  const directValue = typeof variant?.member_name === 'string'
    ? variant.member_name.trim()
    : '';
  if (directValue) return directValue;
  const metadataValue = variant?.metadata?.member_name;
  return typeof metadataValue === 'string' ? metadataValue.trim() : '';
}

function variantLocale(variant) {
  const directValue = typeof variant?.locale === 'string' ? variant.locale.trim() : '';
  if (directValue) return directValue;
  const metadataValue = variant?.metadata?.locale;
  return typeof metadataValue === 'string' ? metadataValue.trim() : '';
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function categoryPayload(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.categories)) return data.categories;
  if (Array.isArray(data?.items)) return data.items;
  return [];
}

function mergeCategories(data) {
  const remote = categoryPayload(data);
  // The backend now only returns populated categories (item_count >= 1), so
  // mirror that here instead of always rendering all DEFAULT_CATEGORIES —
  // an empty category would otherwise show as a dead-end tile.
  if (remote.length === 0) return [];
  return DEFAULT_CATEGORIES.flatMap((fallback) => {
    const match = remote.find((entry) => {
      const slug = typeof entry === 'string'
        ? entry
        : entry?.slug || entry?.id || entry?.category;
      return String(slug || '').toLowerCase() === fallback.slug;
    });
    if (!match) return [];
    if (typeof match === 'string') return [fallback];
    const count = match.count ?? match.total ?? match.item_count;
    if (Number(count) <= 0) return [];
    return [{
      ...fallback,
      label: match.label || match.display_name || match.name || fallback.label,
      description: match.description || fallback.description,
      count,
    }];
  });
}

function itemsPayload(data) {
  if (Array.isArray(data)) return data;
  return Array.isArray(data?.items) ? data.items : [];
}

function nextPageToken(data) {
  if (data?.next_cursor !== null && data?.next_cursor !== undefined && data?.next_cursor !== '') {
    return { type: 'cursor', value: data.next_cursor };
  }
  if (data?.pagination?.has_more) {
    return { type: 'page', value: Number(data.pagination.page || 1) + 1 };
  }
  return null;
}

function totalPayload(data, fallback) {
  const value = data?.total ?? data?.pagination?.total_items;
  return Number.isFinite(Number(value)) ? Number(value) : fallback;
}

export function DownloadsPage({ isAdmin = false }) {
  const location = useLocation();
  const pageRef = useRef(null);
  const sentinelRef = useRef(null);
  const catalogAbortRef = useRef(null);
  const detailsAbortRef = useRef(null);
  const requestKeyRef = useRef('');
  const consumedNavigationRef = useRef('');

  const [categories, setCategories] = useState(DEFAULT_CATEGORIES);
  const [categoriesStatus, setCategoriesStatus] = useState('loading');
  const [categoriesError, setCategoriesError] = useState('');

  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('');

  const [items, setItems] = useState([]);
  const [nextCursor, setNextCursor] = useState(null);
  const [total, setTotal] = useState(null);
  const [catalogStatus, setCatalogStatus] = useState('idle');
  const [catalogError, setCatalogError] = useState('');

  const [selectedItem, setSelectedItem] = useState(null);
  const [details, setDetails] = useState(null);
  const [detailsStatus, setDetailsStatus] = useState('idle');
  const [detailsError, setDetailsError] = useState('');
  const [selectedMember, setSelectedMember] = useState('');
  const [selectedArchitecture, setSelectedArchitecture] = useState('');
  const [selectedVariantId, setSelectedVariantId] = useState('');
  const [downloadStatus, setDownloadStatus] = useState('idle');
  const [downloadError, setDownloadError] = useState('');
  const [uploadMode, setUploadMode] = useState(null);

  const trimmedQuery = query.trim();
  const isShortQuery = trimmedQuery.length === 1;
  const effectiveSearch = debouncedQuery.length >= 2 ? debouncedQuery : '';
  const effectiveCategory = effectiveSearch ? '' : selectedCategory;
  const hasCatalogRequest = Boolean(effectiveSearch || effectiveCategory);

  const loadCategories = useCallback(async () => {
    setCategoriesStatus('loading');
    setCategoriesError('');
    try {
      const data = await requestJson('/api/downloads/categories');
      setCategories(mergeCategories(data));
      setCategoriesStatus('ready');
    } catch (error) {
      setCategories(DEFAULT_CATEGORIES);
      setCategoriesStatus('error');
      setCategoriesError(error.message || 'Categories could not be refreshed.');
    }
  }, []);

  useEffect(() => {
    loadCategories();
  }, [loadCategories]);

  useEffect(() => {
    if (trimmedQuery.length < 2) {
      setDebouncedQuery('');
      return undefined;
    }
    const timer = window.setTimeout(() => setDebouncedQuery(trimmedQuery), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [trimmedQuery]);

  const buildCatalogPath = useCallback((pageToken = null) => {
    // `limit`/`cursor` is Eden's canonical contract. The page parameters keep
    // this view compatible with catalogs deployed during the migration window.
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE),
      page_size: String(PAGE_SIZE),
    });
    if (effectiveSearch) params.set('q', effectiveSearch);
    if (effectiveCategory) params.set('category', effectiveCategory);
    if (pageToken?.type === 'cursor') {
      params.set('cursor', String(pageToken.value));
    } else if (pageToken?.type === 'page') {
      params.set('page', String(pageToken.value));
    }
    return `/api/downloads?${params.toString()}`;
  }, [effectiveCategory, effectiveSearch]);

  const loadFirstPage = useCallback(async () => {
    catalogAbortRef.current?.abort();

    if (!hasCatalogRequest) {
      requestKeyRef.current = '';
      setItems([]);
      setNextCursor(null);
      setTotal(null);
      setCatalogError('');
      setCatalogStatus('idle');
      return;
    }

    const controller = new AbortController();
    const requestKey = `${effectiveSearch}|${effectiveCategory}`;
    catalogAbortRef.current = controller;
    requestKeyRef.current = requestKey;
    setItems([]);
    setNextCursor(null);
    setTotal(null);
    setCatalogError('');
    setCatalogStatus('loading');

    try {
      const data = await requestJson(buildCatalogPath(), { signal: controller.signal });
      if (requestKeyRef.current !== requestKey) return;
      const loadedItems = itemsPayload(data);
      setItems(loadedItems);
      setNextCursor(nextPageToken(data));
      setTotal(totalPayload(data, loadedItems.length));
      setCatalogStatus('ready');
    } catch (error) {
      if (error.name === 'AbortError') return;
      if (requestKeyRef.current !== requestKey) return;
      setCatalogError(error.message || 'Downloads could not be loaded.');
      setCatalogStatus('error');
    }
  }, [buildCatalogPath, effectiveCategory, effectiveSearch, hasCatalogRequest]);

  useEffect(() => {
    loadFirstPage();
    return () => catalogAbortRef.current?.abort();
  }, [loadFirstPage]);

  const loadNextPage = useCallback(async () => {
    if (!nextCursor || !['ready', 'page-error'].includes(catalogStatus)) return;
    const requestKey = requestKeyRef.current;
    const controller = new AbortController();
    catalogAbortRef.current = controller;
    setCatalogStatus('loading-more');
    setCatalogError('');
    try {
      const data = await requestJson(buildCatalogPath(nextCursor), {
        signal: controller.signal,
      });
      if (requestKeyRef.current !== requestKey) return;
      const loadedItems = itemsPayload(data);
      setItems((current) => {
        const seen = new Set(current.map((item) => String(catalogId(item))));
        return [
          ...current,
          ...loadedItems.filter((item) => !seen.has(String(catalogId(item)))),
        ];
      });
      setNextCursor(nextPageToken(data));
      setTotal((current) => totalPayload(data, current));
      setCatalogStatus('ready');
    } catch (error) {
      if (error.name === 'AbortError' || requestKeyRef.current !== requestKey) return;
      setCatalogError(error.message || 'More downloads could not be loaded.');
      setCatalogStatus('page-error');
    }
  }, [buildCatalogPath, catalogStatus, nextCursor]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !nextCursor || catalogStatus !== 'ready') return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadNextPage();
      },
      { root: pageRef.current, rootMargin: '320px 0px', threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [catalogStatus, loadNextPage, nextCursor]);

  const closeDetails = useCallback(() => {
    detailsAbortRef.current?.abort();
    setSelectedItem(null);
    setDetails(null);
    setDetailsStatus('idle');
    setDetailsError('');
    setSelectedMember('');
    setSelectedArchitecture('');
    setSelectedVariantId('');
    setDownloadStatus('idle');
    setDownloadError('');
  }, []);

  const openDetails = useCallback(async (item) => {
    detailsAbortRef.current?.abort();
    const id = catalogId(item);
    setSelectedItem(item);
    setDetails(null);
    setDetailsError('');
    setSelectedMember('');
    setSelectedArchitecture('');
    setSelectedVariantId('');
    setDownloadStatus('idle');
    setDownloadError('');

    if (id === null || id === undefined || id === '') {
      setDetailsStatus('error');
      setDetailsError('This download is missing its catalog identifier.');
      return;
    }

    const controller = new AbortController();
    detailsAbortRef.current = controller;
    setDetailsStatus('loading');
    try {
      const data = await requestJson(`/api/downloads/${encodeURIComponent(id)}`, {
        signal: controller.signal,
      });
      const itemDetails = data?.item
        ? { ...data.item, variants: data.item.variants || data.variants || [] }
        : data;
      const variants = Array.isArray(itemDetails?.variants) ? itemDetails.variants : [];
      setDetails(itemDetails);
      setDetailsStatus('ready');
      if (variants.length > 0) {
        setSelectedMember(variantMember(variants[0]));
        setSelectedArchitecture(variantArchitecture(variants[0]));
        setSelectedVariantId(String(variantId(variants[0])));
      }
    } catch (error) {
      if (error.name === 'AbortError') return;
      setDetailsStatus('error');
      setDetailsError(error.message || 'Download details could not be loaded.');
    }
  }, []);

  useEffect(() => () => detailsAbortRef.current?.abort(), []);

  useEffect(() => {
    if (location.pathname !== '/downloads') return;
    const navigationState = location.state || {};
    const incomingQuery = navigationState.downloadQuery;
    if (typeof incomingQuery === 'string' && incomingQuery.trim().length >= 2) {
      setSelectedCategory('');
      setQuery(incomingQuery);
    }

    const incomingItem = navigationState.openDownload;
    const incomingId = catalogId(incomingItem);
    if (incomingItem && incomingId !== null && incomingId !== undefined) {
      const navigationKey = `${location.key}:${incomingId}`;
      if (consumedNavigationRef.current !== navigationKey) {
        consumedNavigationRef.current = navigationKey;
        openDetails(incomingItem);
      }
    }
  }, [location.key, location.pathname, location.state, openDetails]);

  const variants = useMemo(() => {
    if (Array.isArray(details?.variants)) return details.variants;
    return [];
  }, [details]);

  const memberOptions = useMemo(
    () => [...new Set(variants.map(variantMember))],
    [variants],
  );

  const hasMemberSelector = memberOptions.some(Boolean);

  const memberVariants = useMemo(
    () => (hasMemberSelector
      ? variants.filter((variant) => variantMember(variant) === selectedMember)
      : variants),
    [hasMemberSelector, selectedMember, variants],
  );

  const architectures = useMemo(
    () => [...new Set(memberVariants.map(variantArchitecture))],
    [memberVariants],
  );

  const matchingVariants = useMemo(
    () => memberVariants.filter((variant) => variantArchitecture(variant) === selectedArchitecture),
    [memberVariants, selectedArchitecture],
  );

  const selectedVariant = useMemo(
    () => variants.find((variant) => String(variantId(variant)) === selectedVariantId) || null,
    [selectedVariantId, variants],
  );

  const handleMemberChange = (event) => {
    const member = event.target.value;
    const firstMatch = variants.find((variant) => variantMember(variant) === member);
    setSelectedMember(member);
    setSelectedArchitecture(firstMatch ? variantArchitecture(firstMatch) : '');
    setSelectedVariantId(firstMatch ? String(variantId(firstMatch)) : '');
    setDownloadError('');
  };

  const handleArchitectureChange = (event) => {
    const architecture = event.target.value;
    const firstMatch = memberVariants.find(
      (variant) => variantArchitecture(variant) === architecture,
    );
    setSelectedArchitecture(architecture);
    setSelectedVariantId(firstMatch ? String(variantId(firstMatch)) : '');
    setDownloadError('');
  };

  const handleDownload = async () => {
    const id = variantId(selectedVariant);
    if (id === null || id === undefined || id === '') return;
    setDownloadStatus('loading');
    setDownloadError('');
    try {
      const data = await requestJson(
        `/api/downloads/variants/${encodeURIComponent(id)}/url`,
        { method: 'POST' },
      );
      if (!data?.url) throw new Error('The server did not return a download URL.');

      const downloadUrl = new URL(data.url, window.location.origin);
      if (!['http:', 'https:'].includes(downloadUrl.protocol)) {
        throw new Error('The server returned an unsupported download URL.');
      }

      // S3 is signed with Content-Disposition: attachment. Same-tab navigation
      // avoids popup blockers while keeping the SPA open as the browser starts
      // the file transfer.
      window.location.assign(downloadUrl.href);
      setDownloadStatus('ready');
    } catch (error) {
      setDownloadStatus('error');
      setDownloadError(error.message || 'The download could not be started.');
    }
  };

  const selectedCategoryInfo = categories.find((category) => category.slug === effectiveCategory);
  const resultTitle = effectiveSearch
    ? `Search results for “${effectiveSearch}”`
    : selectedCategoryInfo?.label || '';

  const chooseCategory = (slug) => {
    setQuery('');
    setDebouncedQuery('');
    setSelectedCategory(slug);
    pageRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const handleUploaded = async (result, metadata) => {
    await loadCategories();
    if (result?.created_catalog) {
      const uploadedCategory = metadata?.category || result?.item?.category;
      if (uploadedCategory) {
        const isCurrentCategory = !effectiveSearch && effectiveCategory === uploadedCategory;
        chooseCategory(uploadedCategory);
        if (isCurrentCategory) await loadFirstPage();
        return;
      }
    }
    if (hasCatalogRequest) await loadFirstPage();
  };

  return (
    <div
      ref={pageRef}
      className={styles.page}
      aria-busy={catalogStatus === 'loading' || catalogStatus === 'loading-more'}
    >
      <header className={styles.hero}>
        <div>
          <span className={styles.eyebrow}>Offline software catalog</span>
          <h1 className={styles.title}>Downloads</h1>
          <p className={styles.intro}>
            Find available packages without loading the entire archive. Search across the
            catalog or open a category to begin.
          </p>
        </div>

        <div className={styles.searchWrap} role="search">
          <label className={styles.visuallyHidden} htmlFor="download-search">
            Search downloads
          </label>
          <span className={styles.searchIcon} aria-hidden="true">⌕</span>
          <input
            id="download-search"
            className={styles.searchInput}
            type="search"
            value={query}
            placeholder="Search downloads (2+ characters)"
            autoComplete="off"
            onChange={(event) => setQuery(event.target.value)}
          />
          {query && (
            <button
              type="button"
              className={styles.clearSearch}
              aria-label="Clear download search"
              onClick={() => setQuery('')}
            >
              ×
            </button>
          )}
        </div>
        <div className={styles.searchHint} aria-live="polite">
          {isShortQuery
            ? 'Type one more character to search.'
            : effectiveSearch
              ? `Searching the full catalog for “${effectiveSearch}”.`
              : 'Search starts after two characters.'}
        </div>
      </header>

      <section className={styles.categoriesSection} aria-labelledby="download-categories-title">
        <div className={styles.sectionHeading}>
          <div>
            <h2 id="download-categories-title">Browse by category</h2>
            <p>Only the category you select will be loaded.</p>
          </div>
          {categoriesStatus === 'loading' && (
            <span className={styles.inlineStatus} role="status">Refreshing counts…</span>
          )}
          {isAdmin && (
            <div className={styles.headingActions}>
              <Button variant="ghost" size="sm" onClick={() => setUploadMode('new')}>
                + Upload new app
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setUploadMode('version')}>
                + Add version
              </Button>
            </div>
          )}
        </div>

        {categoriesStatus === 'error' && (
          <div className={styles.categoryNotice} role="status">
            <span>{categoriesError} You can still browse the catalog.</span>
            <button type="button" onClick={loadCategories}>Retry counts</button>
          </div>
        )}

        <div className={styles.categoryGrid}>
          {categories.map((category) => (
            <button
              key={category.slug}
              type="button"
              className={`${styles.categoryCard} ${
                effectiveCategory === category.slug ? styles.categoryCardActive : ''
              }`}
              aria-pressed={effectiveCategory === category.slug}
              onClick={() => chooseCategory(category.slug)}
            >
              <span className={styles.categoryIcon} aria-hidden="true">{category.icon}</span>
              <span className={styles.categoryCopy}>
                <span className={styles.categoryName}>{category.label}</span>
                <span className={styles.categoryDescription}>{category.description}</span>
              </span>
              {category.count !== undefined && category.count !== null && (
                <span className={styles.categoryCount}>
                  {Number(category.count).toLocaleString()}
                </span>
              )}
              <span className={styles.categoryArrow} aria-hidden="true">→</span>
            </button>
          ))}
        </div>
      </section>

      <section className={styles.resultsSection} aria-labelledby="download-results-title">
        {!hasCatalogRequest && (
          <div className={styles.idleState}>
            <span className={styles.idleIcon} aria-hidden="true">↓</span>
            <h2 id="download-results-title">Choose where to start</h2>
            <p>Select a category above or search with at least two characters.</p>
          </div>
        )}

        {hasCatalogRequest && (
          <>
            <div className={styles.resultsHeading}>
              <div>
                <h2 id="download-results-title">{resultTitle}</h2>
                <p aria-live="polite">
                  {catalogStatus === 'loading'
                    ? 'Loading downloads…'
                    : `${total ?? items.length} ${total === 1 ? 'item' : 'items'} found`}
                </p>
              </div>
            </div>

            {catalogStatus === 'loading' && (
              <div className={styles.loadingGrid} role="status" aria-label="Loading downloads">
                {Array.from({ length: 8 }, (_, index) => (
                  <span key={index} className={styles.skeleton} aria-hidden="true" />
                ))}
              </div>
            )}

            {catalogStatus === 'error' && (
              <div className={styles.messageState} role="alert">
                <span className={styles.messageIcon} aria-hidden="true">!</span>
                <h3>Downloads are unavailable</h3>
                <p>{catalogError}</p>
                <Button variant="secondary" onClick={loadFirstPage}>Try again</Button>
              </div>
            )}

            {catalogStatus !== 'loading' && catalogStatus !== 'error' && items.length === 0 && (
              <div className={styles.messageState} role="status">
                <span className={styles.messageIcon} aria-hidden="true">⌕</span>
                <h3>No matching downloads</h3>
                <p>Try another term or browse one of the categories.</p>
              </div>
            )}

            {items.length > 0 && (
              <div className={styles.resultsGrid}>
                {items.map((item, index) => (
                  <DownloadCard
                    key={catalogId(item) ?? `${item.name || item.display_name}-${index}`}
                    item={item}
                    onSelect={openDetails}
                  />
                ))}
              </div>
            )}

            <div ref={sentinelRef} className={styles.sentinel} aria-hidden="true" />

            {catalogStatus === 'loading-more' && (
              <div className={styles.pageStatus} role="status">
                <span className={styles.spinner} aria-hidden="true" />
                Loading more downloads…
              </div>
            )}

            {catalogStatus === 'page-error' && (
              <div className={styles.pageError} role="alert">
                <span>{catalogError}</span>
                <Button variant="secondary" size="sm" onClick={loadNextPage}>Try again</Button>
              </div>
            )}

            {catalogStatus === 'ready' && items.length > 0 && !nextCursor && (
              <p className={styles.endMessage}>You’ve reached the end of these results.</p>
            )}
          </>
        )}
      </section>

      <Modal
        open={Boolean(selectedItem)}
        onClose={closeDetails}
        title={details?.display_name || details?.name || selectedItem?.display_name || selectedItem?.name || 'Download'}
        subtitle="Choose an architecture and version"
        wide
        footer={(
          <>
            <Button variant="secondary" onClick={closeDetails}>Close</Button>
            <Button
              onClick={handleDownload}
              loading={downloadStatus === 'loading'}
              disabled={detailsStatus !== 'ready' || !selectedVariant}
            >
              Download selected file
            </Button>
          </>
        )}
      >
        {detailsStatus === 'loading' && (
          <div className={styles.modalLoading} role="status">
            <span className={styles.spinner} aria-hidden="true" />
            Loading available files…
          </div>
        )}

        {detailsStatus === 'error' && (
          <div className={styles.modalError} role="alert">
            <p>{detailsError}</p>
            <Button variant="secondary" size="sm" onClick={() => openDetails(selectedItem)}>
              Try again
            </Button>
          </div>
        )}

        {detailsStatus === 'ready' && variants.length === 0 && (
          <div className={styles.modalError} role="status">
            <p>No downloadable files are currently attached to this catalog item.</p>
          </div>
        )}

        {detailsStatus === 'ready' && variants.length > 0 && (
          <div className={styles.variantPanel}>
            {(details?.description || selectedItem?.description) && (
              <p className={styles.modalDescription}>
                {details?.description || selectedItem?.description}
              </p>
            )}

            <div className={styles.selectorGrid}>
              {hasMemberSelector && (
                <label className={styles.field}>
                  <span>Product/component</span>
                  <select value={selectedMember} onChange={handleMemberChange}>
                    {memberOptions.map((member) => (
                      <option key={member || '__general__'} value={member}>
                        {member || 'General'}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <label className={styles.field}>
                <span>Architecture</span>
                <select value={selectedArchitecture} onChange={handleArchitectureChange}>
                  {architectures.map((architecture) => (
                    <option key={architecture} value={architecture}>{architecture}</option>
                  ))}
                </select>
              </label>

              <label className={styles.field}>
                <span>Version</span>
                <select
                  value={selectedVariantId}
                  onChange={(event) => {
                    setSelectedVariantId(event.target.value);
                    setDownloadError('');
                  }}
                >
                  {matchingVariants.map((variant) => (
                    <option key={variantId(variant)} value={String(variantId(variant))}>
                      {variantVersion(variant)}
                      {variantLocale(variant) ? ` · Locale: ${variantLocale(variant)}` : ''}
                      {(variant.os || variant.operating_system)
                        ? ` · ${variant.os || variant.operating_system}`
                        : ''}
                      {variant.file_type ? ` · ${String(variant.file_type).toUpperCase()}` : ''}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {selectedVariant && (
              <div className={styles.fileSummary}>
                <div className={styles.fileMark} aria-hidden="true">↓</div>
                <div className={styles.fileCopy}>
                  <strong>
                    {selectedVariant.filename || selectedVariant.file_name || 'Download file'}
                  </strong>
                  <span>
                    {[
                      variantArchitecture(selectedVariant),
                      variantVersion(selectedVariant),
                      variantLocale(selectedVariant) ? `Locale: ${variantLocale(selectedVariant)}` : '',
                      formatBytes(selectedVariant.size_bytes ?? selectedVariant.size),
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                  {(selectedVariant.checksum_sha256 || selectedVariant.sha256) && (
                    <code title={selectedVariant.checksum_sha256 || selectedVariant.sha256}>
                      SHA-256 {selectedVariant.checksum_sha256 || selectedVariant.sha256}
                    </code>
                  )}
                </div>
              </div>
            )}

            {downloadError && (
              <p className={styles.downloadError} role="alert">{downloadError}</p>
            )}
          </div>
        )}
      </Modal>

      <DownloadUploadModal
        open={Boolean(uploadMode)}
        mode={uploadMode}
        categories={categories}
        onClose={() => setUploadMode(null)}
        onUploaded={handleUploaded}
      />
    </div>
  );
}
