import React from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import styles from './TopBar.module.css';

// Inline SVG icons — loaded as React components so CSS `color` tints them.
// Drop your SVGs in frontend/public/images/ with these names.
// They MUST use fill="currentColor" (not fill="#000000").
// If the file is missing the icon simply won't render — text label still shows.

function TabIcon({ src, className }) {
  const [svgContent, setSvgContent] = React.useState('');
  React.useEffect(() => {
    if (!src) return;
    fetch(src)
      .then(r => r.text())
      .then(text => {
        // Strip XML declaration and DOCTYPE, keep only the <svg>...</svg>
        const match = text.match(/<svg[\s\S]*<\/svg>/i);
        setSvgContent(match ? match[0] : '');
      })
      .catch(() => setSvgContent(''));
  }, [src]);

  if (!svgContent) return null;
  return (
    <span
      className={className}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: svgContent }}
    />
  );
}

const TABS = [
  { path: '/',           label: 'Home',      icon: '/images/home.svg'      },
  { path: '/apps',       label: 'Web Apps',  icon: '/images/webapps.svg'   },
  { path: '/scripts',    label: 'Scripts',   icon: '/images/scripts.svg'   },
  { path: '/downloads',  label: 'Downloads', icon: '/images/downloads.svg' },
];

export function TopBar({ username, theme, onToggleTheme }) {
  const [modalOpen, setModalOpen] = React.useState(false);

  React.useEffect(() => {
    const onLock   = () => setModalOpen(true);
    const onUnlock = () => setModalOpen(false);
    window.addEventListener('eden:lockScroll',   onLock);
    window.addEventListener('eden:unlockScroll', onUnlock);
    return () => {
      window.removeEventListener('eden:lockScroll',   onLock);
      window.removeEventListener('eden:unlockScroll', onUnlock);
    };
  }, []);
  const navigate  = useNavigate();
  const { pathname } = useLocation();

  return (
    <header className={styles.topbar}>
      {/* Left — logo + username */}
      <div className={styles.left}>
        <div className={styles.logoMark}>
          <span className={styles.lb}>[</span>
          <span className={styles.ls}>/</span>
          <span className={styles.lb}>]</span>
        </div>
        <div className={styles.logoText}>
          <span className={styles.portalName}>Eden</span>
        </div>
        {username && (
          <span className={styles.hello}>Hello, {username}</span>
        )}
      </div>

      {/* Center — tab navigation */}
      <nav className={styles.nav} style={modalOpen ? {pointerEvents:'none',opacity:.5} : {}}>
        {TABS.map(tab => (
          <button
            key={tab.path}
            type="button"
            className={`${styles.tabBtn} ${pathname === tab.path ? styles.active : ''}`}
            onClick={() => navigate(tab.path)}
          >
            <TabIcon src={tab.icon} className={styles.tabIcon} />
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Right — theme toggle */}
      <div className={styles.right}>
        <button type="button" className={styles.themeBtn} onClick={onToggleTheme} title="Toggle theme">
          {theme === 'dark' ? '☀️ Light' : '🌙 Dark'}
        </button>
      </div>
    </header>
  );
}
