import React, { useState, useCallback, useEffect, useRef } from 'react';
import styles from './Modal.module.css';
import { ConfirmModal } from '../ConfirmModal/ConfirmModal.jsx';

export function Modal({ open, onClose, title, subtitle, wide, confirmLeave, footer, children }) {
  const [showConfirm, setShowConfirm] = useState(false);
  const bodyRef = useRef(null);

  // Lock/unlock snap-container tab switching while modal is open
  useEffect(() => {
    if (open) {
      window.dispatchEvent(new CustomEvent('eden:lockScroll'));
    } else {
      window.dispatchEvent(new CustomEvent('eden:unlockScroll'));
    }
    return () => {
      // Always unlock when unmounted
      window.dispatchEvent(new CustomEvent('eden:unlockScroll'));
    };
  }, [open]);

  // Stop wheel events from bubbling out of the modal body to the snap container
  // This lets the modal body scroll independently
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const stop = (e) => { e.stopPropagation(); };
    el.addEventListener('wheel', stop, { passive: true });
    return () => el.removeEventListener('wheel', stop);
  }, [open]);

  const handleBackdropClick = useCallback(() => {
    if (confirmLeave) {
      setShowConfirm(true);
    } else {
      onClose();
    }
  }, [confirmLeave, onClose]);

  if (!open) return null;

  return (
    <>
      <div className={styles.backdrop} onClick={handleBackdropClick}>
        <div
          className={`${styles.modal} ${wide ? styles.wide : ''}`}
          data-modal="true"
          onClick={e => e.stopPropagation()}
        >
          <div className={styles.header}>
            <div className={styles.titleArea}>
              <span className={styles.title}>{title}</span>
              {subtitle && <span className={styles.subtitle}>{subtitle}</span>}
            </div>
            <button type="button" className={styles.closeBtn} onClick={handleBackdropClick}>✕</button>
          </div>

          <div className={styles.body} ref={bodyRef}>{children}</div>

          {footer && <div className={styles.footer}>{footer}</div>}
        </div>
      </div>

      {showConfirm && (
        <ConfirmModal
          message="You have unsaved changes. Are you sure you want to leave?"
          onConfirm={() => { setShowConfirm(false); onClose(); }}
          onCancel={() => setShowConfirm(false)}
        />
      )}
    </>
  );
}
