import { useState, useEffect, useCallback, useRef } from 'react';
import toast from 'react-hot-toast';
import { fetchMyRequests, fetchMyHistory } from '../api/index.js';

const SEEN_KEY   = 'eden:myRequests:seenResolved';
const POLL_MS    = 20000; // a workflow finishing is minutes-granularity at best; no need for anything tighter
const SEEN_CAP   = 500;   // bound localStorage growth for a long-lived browser tab/profile

const itemKey = item => `${item.kind}:${item.id}`;

function loadSeen() {
  try { return new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')); }
  catch { return new Set(); }
}
function saveSeen(set) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify([...set].slice(-SEEN_CAP))); }
  catch { /* private-browsing/storage-full -- notifications just won't dedupe across reloads */ }
}

function itemLabel(item) {
  return item.kind === 'webapp' ? item.name : `${item.team}/${item.script_name}`;
}

// Best-effort human message for an item that just left "my requests" --
// `item` here is whatever we could find (history record if the lookup
// succeeded, otherwise the last-known active record) so this degrades
// gracefully instead of throwing on a missing field.
function resolvedMessage(item) {
  const label = itemLabel(item);
  if (item.kind === 'script_run') {
    if (item.status === 'rejected') return `❌ ${label} was rejected`;
    if (item.workflow_phase === 'Succeeded') return `✅ ${label} completed successfully`;
    if (item.workflow_phase) return `⚠️ ${label} finished: ${item.workflow_phase}`;
    return `${label} was approved`;
  }
  return item.status === 'rejected' ? `❌ ${label} was rejected` : `✅ ${label} was approved`;
}

/**
 * Polls "my active requests", toasting once (ever, per browser -- tracked
 * in localStorage) for each item that disappears from that list, since
 * that's exactly what "resolved" means here. On a resolve, does one extra
 * fetch of "my history" to get the real final status/outcome for the
 * toast text -- best-effort, falls back to the last-known active record
 * if that lookup fails so a transient error never blocks the notification.
 */
export function useMyRequests() {
  const [active, setActive] = useState([]);
  const [loading, setLoading] = useState(true);
  const prevItemsRef = useRef(null); // Map<key, item> | null until first successful fetch

  const refresh = useCallback(async () => {
    try {
      const fresh = await fetchMyRequests();
      const freshMap = new Map(fresh.map(it => [itemKey(it), it]));

      if (prevItemsRef.current) {
        const seen = loadSeen();
        const newlyResolvedKeys = [...prevItemsRef.current.keys()]
          .filter(k => !freshMap.has(k) && !seen.has(k));

        if (newlyResolvedKeys.length > 0) {
          newlyResolvedKeys.forEach(k => seen.add(k));
          saveSeen(seen);

          let historyMap = new Map();
          try {
            const history = await fetchMyHistory();
            historyMap = new Map(history.map(it => [itemKey(it), it]));
          } catch { /* fall back to the last-known active record below */ }

          newlyResolvedKeys.forEach(k => {
            const item = historyMap.get(k) || prevItemsRef.current.get(k);
            toast.success(resolvedMessage(item));
          });
        }
      }

      prevItemsRef.current = freshMap;
      setActive(fresh);
    } catch {
      // transient failure -- keep showing the last-known state
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return { active, loading, refresh };
}
