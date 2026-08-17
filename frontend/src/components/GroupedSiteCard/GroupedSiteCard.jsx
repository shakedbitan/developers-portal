import React, { useState, useEffect, useRef } from 'react';
import styles from './GroupedSiteCard.module.css';

const CARD_H = 124; // main card height px — must match CSS, and must equal
                     // SiteCard's collapsed height (76px imgWrap + 48px label)
                     // exactly, or align-items:flex-end on the shared .grid
                     // row has to compensate and rows drift out of line.
const ROW_H  = 38;  // env row height px
const GAP    = 14;  // gap between rows, and between the card and the first row

/**
 * GroupedSiteCard
 * Shows a stacked card for grouped starred apps.
 * Clicking expands env rows upward with staggered animation.
 *
 * Props:
 *   displayName  — string shown on the card (group_display_name or capitalized group_name)
 *   imageUrl     — image for the top card
 *   members      — array of { id, url, env_label, image_url, env_color }
 *                  env_color is a resolved hex string (or undefined) —
 *                  picked in the Submit/Edit Web App modal, used here as
 *                  the bullet color and the row's frame color.
 *   small        — boolean (home screen small variant)
 */
export function GroupedSiteCard({ displayName, imageUrl, members, small = false }) {
  const [open, setOpen]   = useState(false);
  const wrapRef           = useRef(null);

  // Close when clicking outside
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const count       = members.length;
  const expandedH   = CARD_H + (ROW_H + GAP) * count + GAP;
  // Collapsed height is always exactly CARD_H — same box, every time,
  // regardless of member count. No per-count "peek" reserve here: that
  // used to (a) grow the wrap taller than a solo SiteCard's, which is
  // what was fighting the shared row's alignment, and (b) sit a dark
  // blurred layer directly behind the (semi-transparent) icon tile,
  // bleeding through and shifting its color. The count badge already
  // shows there's a group; that's enough of a "there's more here" cue.
  const collapsedH  = CARD_H;

  const wrapStyle = {
    height: open ? expandedH : collapsedH,
  };

  return (
    <div
      ref={wrapRef}
      className={`${styles.wrap} ${small ? styles.small : ''} ${open ? styles.open : ''}`}
      style={wrapStyle}
    >
      {/* Environment rows — spread upward when open */}
      {members.map((member, i) => {
        const bottomPos = open
          ? CARD_H + GAP + (count - 1 - i) * (ROW_H + GAP)
          : 0;
        const delay     = open ? (count - 1 - i) * 20 : i * 15;
        const opacity   = open ? 1 : 0;

        return (
          <a
            key={member.id}
            href={member.url}
            className={styles.envRow}
            data-no-dnd="true"
            style={{
              bottom:     bottomPos,
              opacity:    opacity,
              zIndex:     count - i,
              transitionDelay: `${delay}ms`,
              pointerEvents: open ? 'all' : 'none',
              borderColor: member.env_color || 'transparent',
            }}
            onClick={e => e.stopPropagation()}
          >
            <span className={styles.envBullet} style={{ background: member.env_color || 'var(--text-muted)' }} />
            <span className={styles.envLabel}>{member.env_label || member.url}</span>
          </a>
        );
      })}

      {/* Main card */}
      <div
        className={styles.mainCard}
        onClick={() => setOpen(o => !o)}
      >
        {/* Count badge */}
        {!open && (
          <span className={styles.badge}>{count}</span>
        )}

        {/* Logo */}
        <div className={styles.imgWrap}>
          <img
            src={imageUrl || '/icons/placeholder.svg'}
            alt={displayName}
            onError={e => { e.target.src = '/icons/placeholder.svg'; }}
          />
        </div>

        {/* Name + close hint */}
        <div className={styles.label}>
          <span className={styles.name}>{displayName}</span>
          {open && <span className={styles.closeHint}>click to close</span>}
        </div>
      </div>
    </div>
  );
}
