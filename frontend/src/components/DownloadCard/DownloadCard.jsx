import React from 'react';
import styles from './DownloadCard.module.css';

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

function itemName(item) {
  return item?.display_name || item?.name || item?.filename || item?.file_name || 'Untitled download';
}

export function DownloadCard({ item, onSelect }) {
  const name = itemName(item);
  const variantCount = Number(item?.variant_count ?? item?.variants_count ?? item?.variants?.length);
  const size = formatBytes(item?.size_bytes ?? item?.total_size_bytes ?? item?.size);
  const metadata = [
    Number.isFinite(variantCount) && variantCount > 0
      ? `${variantCount} ${variantCount === 1 ? 'file' : 'variants'}`
      : '',
    item?.latest_version
      ? `v${item.latest_version}`
      : Array.isArray(item?.versions) && item.versions.length > 0
        ? `v${item.versions[0]}`
        : '',
    size,
  ].filter(Boolean);

  return (
    <button
      type="button"
      className={styles.card}
      onClick={() => onSelect(item)}
      aria-label={`View download options for ${name}`}
    >
      <span className={styles.content}>
        <span className={styles.topLine}>
          <strong className={styles.name} title={name}>{name}</strong>
        </span>
        {item?.description && (
          <span className={styles.description}>{item.description}</span>
        )}
        <span className={styles.meta}>
          {metadata.length > 0 ? metadata.join(' · ') : 'Select to view available files'}
        </span>
      </span>

      <span className={styles.arrow} aria-hidden="true">→</span>
    </button>
  );
}
