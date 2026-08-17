import React, { useState, useEffect } from 'react';
import styles from './LoadingOverlay.module.css';

export function LoadingOverlay() {
  const [show, setShow]   = useState(false);
  const [label, setLabel] = useState('');

  useEffect(() => {
    const handleClick = (e) => {
      const a = e.target.closest('a[href]');
      if (!a) return;
      const href = a.getAttribute('href');
      if (!href || (!href.startsWith('http://') && !href.startsWith('https://'))) return;
      if (a.target === '_blank') return;
      try { setLabel(new URL(href).hostname); } catch { setLabel(href); }
      setShow(true);
    };
    const handlePageShow = () => setShow(false);

    document.addEventListener('click', handleClick, true);
    window.addEventListener('pageshow', handlePageShow);
    return () => {
      document.removeEventListener('click', handleClick, true);
      window.removeEventListener('pageshow', handlePageShow);
    };
  }, []);

  if (!show) return null;

  return (
    <div className={styles.overlay}>
      <div className={styles.box}>
        <div className={styles.spinner} />
        <div className={styles.text}>Opening {label}<span className={styles.dots}>...</span></div>
      </div>
    </div>
  );
}
