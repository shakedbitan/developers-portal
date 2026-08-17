/**
 * utils.js — shared utility functions
 */

// Deterministic color from a tag string
const TAG_COLORS = [
  { bg: 'rgba(79,255,176,.12)',  color: '#4fffb0', border: 'rgba(79,255,176,.25)'  },
  { bg: 'rgba(0,201,255,.12)',   color: '#00c9ff', border: 'rgba(0,201,255,.25)'   },
  { bg: 'rgba(245,158,11,.12)',  color: '#fbbf24', border: 'rgba(245,158,11,.25)'  },
  { bg: 'rgba(167,139,250,.12)', color: '#a78bfa', border: 'rgba(167,139,250,.25)' },
  { bg: 'rgba(244,114,182,.12)', color: '#f472b6', border: 'rgba(244,114,182,.25)' },
  { bg: 'rgba(52,211,153,.12)',  color: '#34d399', border: 'rgba(52,211,153,.25)'  },
  { bg: 'rgba(251,146,60,.12)',  color: '#fb923c', border: 'rgba(251,146,60,.25)'  },
];

export function tagStyle(tag) {
  const idx = [...tag].reduce((acc, c) => acc + c.charCodeAt(0), 0) % TAG_COLORS.length;
  const { bg, color, border } = TAG_COLORS[idx];
  return { background: bg, color, border: `1px solid ${border}`, borderRadius: '999px' };
}

/**
 * Format a file size in bytes to human-readable string
 */
export function formatBytes(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(1)} ${units[i]}`;
}

/**
 * Debounce a function
 */
export function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}