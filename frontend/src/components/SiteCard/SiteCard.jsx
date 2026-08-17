import React from 'react';
import styles from './SiteCard.module.css';
import { tagStyle } from '../../utils.js';

export function SiteCard({ site, isStarred, onToggleStar, isAdmin, onEdit, onDelete, small = false }) {
  const handleStar = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onToggleStar(site);
  };

  const handleEdit = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onEdit(site);
  };

  const handleDelete = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onDelete(site);
  };

  return (
    <div className={`${styles.wrap} ${small ? styles.small : ''}`}>
      {/* Star button hidden on home screen (small=true), shown only on WebApps */}
      {!small && (
        <button type="button" className={`${styles.starBtn} ${isStarred ? styles.starred : ''}`}
                onClick={handleStar} title={isStarred ? 'Unstar' : 'Star'}>★</button>
      )}
      {isAdmin && (
        <>
          <button type="button" className={styles.editBtn} onClick={handleEdit}>Edit</button>
          <button type="button" className={styles.delBtn}  onClick={handleDelete}>Del</button>
        </>
      )}

      <a className={styles.card} href={site.url}>
        <div className={styles.imgWrap}>
          {!small && site.tags?.[0] && (
            <div className={styles.banner}
                 style={{ background: site.banner_color || '#888' }}>
              {site.tags[0]}
            </div>
          )}
          <img
            src={site.image_url || '/icons/placeholder.svg'}
            alt={site.name}
            onError={e => { e.target.src = '/icons/placeholder.svg'; }}
          />
        </div>
        <div className={styles.label}>
          <span className={styles.name}>{site.name}</span>
        </div>
      </a>
    </div>
  );
}
