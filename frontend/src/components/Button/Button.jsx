import React from 'react';
import styles from './Button.module.css';

export function Button({ variant = 'primary', size = 'md', disabled, loading, onClick, type = 'button', children, className = '' }) {
  return (
    <button
      type={type}
      className={`${styles.btn} ${styles[variant]} ${styles[size]} ${className}`}
      disabled={disabled || loading}
      onClick={onClick}
    >
      {loading ? <span className={styles.spinner} /> : children}
    </button>
  );
}