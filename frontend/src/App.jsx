import React, { useState, useEffect, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { TopBar }        from './components/TopBar/TopBar.jsx';
import { SearchBar }     from './components/SearchBar/SearchBar.jsx';
import { HomePage }      from './pages/Home/HomePage.jsx';
import { WebAppsPage }   from './pages/WebApps/WebAppsPage.jsx';
import { DownloadsPage } from './pages/Downloads/DownloadsPage.jsx';
import { ScriptsPage }   from './pages/Scripts/ScriptsPage.jsx';
import { useAuth }       from './hooks/useAuth.js';
import { useTheme }      from './hooks/useTheme.js';
import { useStars }      from './hooks/useStars.js';
import { fetchSites, fetchScripts } from './api/index.js';
import { LoadingOverlay } from './components/LoadingOverlay/LoadingOverlay.jsx';
import styles from './App.module.css';

const TAB_ORDER = ['/', '/apps', '/scripts', '/downloads'];

export default function App() {
  const { user }          = useAuth();
  const { theme, toggle } = useTheme();
  const { starredSites, starredIds, toggleStar, reorder, refresh: refreshStars } = useStars();
  const navigate          = useNavigate();
  const location          = useLocation();
  const scrollRef         = useRef(null);
  const isScrolling       = useRef(false);
  const scrollTimeout     = useRef(null);
  const scrollLocked      = useRef(false);

  const [sites,   setSites]   = useState([]);
  const [scripts, setScripts] = useState({});
  const [blurred, setBlurred] = useState(false);

  // ── Scroll lock ───────────────────────────────────────────────────────────
  // eden:dragStart  — fired by drag handlers. Locks wheel snap AND hides overflow
  //                   on the snap container so pointer movement can't scroll it.
  // eden:dragEnd    — fired on drag end. Restores everything.
  // eden:lockScroll — fired by modals. Locks wheel snap only (searchbar hides).
  // eden:unlockScroll — fired by modals on close.
  useEffect(() => {
    // Always read scrollRef.current fresh — captured at mount it may be null
    const getContainer = () => scrollRef.current;

    const onDragStart = () => {
      scrollLocked.current = true;
      const c = getContainer();
      if (c) c.style.overflow = 'hidden';
    };
    const onDragEnd = () => {
      scrollLocked.current = false;
      const c = getContainer();
      if (c) c.style.overflow = '';
    };
    const onModalOpen  = () => { scrollLocked.current = true;  };
    const onModalClose = () => { scrollLocked.current = false; };

    window.addEventListener('eden:dragStart',   onDragStart);
    window.addEventListener('eden:dragEnd',     onDragEnd);
    window.addEventListener('eden:lockScroll',  onModalOpen);
    window.addEventListener('eden:unlockScroll', onModalClose);
    return () => {
      window.removeEventListener('eden:dragStart',   onDragStart);
      window.removeEventListener('eden:dragEnd',     onDragEnd);
      window.removeEventListener('eden:lockScroll',  onModalOpen);
      window.removeEventListener('eden:unlockScroll', onModalClose);
    };
  }, []);

  useEffect(() => {
    fetchSites().then(d => setSites(Array.isArray(d) ? d : [])).catch(() => {});
    fetchScripts().then(d => setScripts(d && typeof d === 'object' ? d : {})).catch(() => {});
  }, []);

  // Programmatic scroll when tab clicked
  useEffect(() => {
    const idx = TAB_ORDER.indexOf(location.pathname);
    if (idx < 0 || !scrollRef.current) return;
    const section = scrollRef.current.children[idx];
    if (!section) return;
    isScrolling.current = true;
    clearTimeout(scrollTimeout.current);
    const container = scrollRef.current;
    const start = container.scrollTop;
    const end   = section.offsetTop;
    const dur   = 20;
    const startTime = performance.now();
    const ease = t => t < 0.5 ? 2*t*t : -1+(4-2*t)*t;
    const tick = (now) => {
      const elapsed  = now - startTime;
      const progress = Math.min(elapsed / dur, 1);
      container.scrollTop = start + (end - start) * ease(progress);
      if (progress < 1) {
        requestAnimationFrame(tick);
      } else {
        scrollTimeout.current = setTimeout(() => { isScrolling.current = false; }, 100);
      }
    };
    requestAnimationFrame(tick);
  }, [location.pathname]);

  // Wheel → snap tabs. Respects inner page scroll and drag/modal lock.
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;

    let lastGesture = 0;
    const COOLDOWN  = 1000;

    const snapTo = (dir) => {
      const now = Date.now();
      if (isScrolling.current || now - lastGesture < COOLDOWN) return;
      lastGesture = now;
      const currentIdx = TAB_ORDER.indexOf(location.pathname);
      const nextIdx = Math.max(0, Math.min(TAB_ORDER.length - 1, currentIdx + dir));
      if (nextIdx === currentIdx) return;
      navigate(TAB_ORDER[nextIdx], { replace: true });
    };

    const onWheel = (e) => {
      if (scrollLocked.current) {
        // During drag — always block (drag is never inside a modal)
        // During modal open — only block if NOT scrolling inside the modal
        const inModal = !!e.target.closest('[data-modal="true"]');
        if (!inModal) e.preventDefault();
        return;
      }

      const page  = container.children[TAB_ORDER.indexOf(location.pathname)];
      const inner = page?.querySelector('[class*="page"]');

      if (inner && inner.scrollHeight > inner.clientHeight + 2) {
        const atTop    = inner.scrollTop <= 1;
        const atBottom = inner.scrollTop + inner.clientHeight >= inner.scrollHeight - 1;
        if (e.deltaY > 0 && !atBottom) return;
        if (e.deltaY < 0 && !atTop)    return;
      }
      e.preventDefault();
      snapTo(e.deltaY > 0 ? 1 : -1);
    };

    let touchStartY = 0;
    const onTouchStart = (e) => { touchStartY = e.touches[0].clientY; };
    const onTouchEnd   = (e) => {
      if (scrollLocked.current) return;
      const delta = touchStartY - e.changedTouches[0].clientY;
      if (Math.abs(delta) > 30) snapTo(delta > 0 ? 1 : -1);
    };

    container.addEventListener('wheel',      onWheel,      { passive: false });
    container.addEventListener('touchstart', onTouchStart, { passive: true  });
    container.addEventListener('touchend',   onTouchEnd,   { passive: true  });
    return () => {
      container.removeEventListener('wheel',      onWheel);
      container.removeEventListener('touchstart', onTouchStart);
      container.removeEventListener('touchend',   onTouchEnd);
    };
  }, [location.pathname, navigate]);

  // Blur background based on scroll position
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    const onScroll = () => {
      setBlurred(container.scrollTop > window.innerHeight * 0.2);
    };
    container.addEventListener('scroll', onScroll, { passive: true });
    return () => container.removeEventListener('scroll', onScroll);
  }, []);

  return (
    <>
      <div className={`bg-image ${blurred ? 'blurred' : ''}`} />
      <div className={`bg-overlay ${blurred ? 'dimmed' : ''}`} />
      <div className="noise" />
      <LoadingOverlay />

      <TopBar username={user?.username} theme={theme} onToggleTheme={toggle} />
      <SearchBar sites={sites} scripts={scripts} />

      <div ref={scrollRef} className={styles.snapContainer}>
        <section className={styles.snapSection}>
          <HomePage
            starredSites={starredSites}
            starredIds={starredIds}
            onToggleStar={toggleStar}
            onReorder={reorder}
          />
        </section>

        <section className={styles.snapSection}>
          <WebAppsPage
            sites={sites}
            setSites={setSites}
            starredSites={starredSites}
            starredIds={starredIds}
            onToggleStar={toggleStar}
            onReorder={reorder}
            onSitesChanged={refreshStars}
            isAdmin={user?.is_admin}
          />
        </section>

        <section className={styles.snapSection}>
          <ScriptsPage
            scripts={scripts}
            setScripts={setScripts}
            isAdmin={user?.is_admin}
          />
        </section>

        <section className={styles.snapSection}>
          <DownloadsPage isAdmin={user?.is_admin} />
        </section>
      </div>
    </>
  );
}
