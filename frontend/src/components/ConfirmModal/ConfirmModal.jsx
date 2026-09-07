import React from 'react';
import styles from './ConfirmModal.module.css';

export function ConfirmModal({ message, onConfirm, onCancel, confirmLabel = 'Leave', cancelLabel = 'Stay' }) {
  return (
    <div className={styles.backdrop} onClick={onCancel}>
      <div className={styles.box} onClick={e => e.stopPropagation()}>
        <p className={styles.msg}>{message}</p>
        <div className={styles.actions}>
          <button className={styles.cancelBtn} onClick={onCancel}>{cancelLabel}</button>
          <button className={styles.confirmBtn} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
