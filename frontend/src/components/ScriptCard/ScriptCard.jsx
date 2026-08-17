import React from 'react';
import styles from './ScriptCard.module.css';

export function ScriptCard({ script, team, onClick }) {
  return (
    <button type="button" className={styles.card} onClick={() => onClick(script, team)}>
      <div className={styles.imgWrap}>
        <img
          src={`/api/scripts/${team}/${script.folder_name}/logo`}
          alt={script.name}
          onError={e => { e.target.src = '/icons/placeholder.svg'; }}
        />
      </div>
      <div className={styles.label}>
        <span className={styles.name}>{script.name}</span>
        <span className={styles.desc}>{script.description}</span>
        <div className={styles.badges}>
          <span className={`${styles.langBadge} ${styles[`lang_${script.language}`]}`}>
            {script.language}
          </span>
          {script.approval_required && (
            <span className={styles.approvalBadge}>approval</span>
          )}
        </div>
      </div>
    </button>
  );
}
