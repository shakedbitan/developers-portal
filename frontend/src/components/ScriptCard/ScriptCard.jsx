import React from 'react';
import styles from './ScriptCard.module.css';

export function ScriptCard({ script, team, onClick }) {
  return (
    <button type="button" className={styles.card} onClick={() => onClick(script, team)}>
      <div className={styles.chrome}>
        <span className={`${styles.dot} ${styles.dotR}`} />
        <span className={`${styles.dot} ${styles.dotY}`} />
        <span className={`${styles.dot} ${styles.dotG}`} />
      </div>

      <div className={styles.body}>
        <div className={styles.top}>
          <span className={styles.imgTile}>
            <img
              src={`/api/scripts/${team}/${script.folder_name}/logo`}
              alt=""
              onError={e => { e.target.src = '/icons/placeholder.svg'; }}
            />
          </span>
          <span className={styles.path}>~/scripts/{script.folder_name}</span>
        </div>

        <div className={styles.prompt}>
          <span className={styles.sigil}>$</span> <span className={styles.name}>{script.name}</span>
          <span className={styles.cursor} aria-hidden="true" />
        </div>

        <div className={styles.desc}>{script.description}</div>
      </div>
    </button>
  );
}
