import { useState, useEffect, useCallback } from 'react';
import toast from 'react-hot-toast';
import { fetchStars, addStar, removeStar, reorderStars } from '../api/index.js';

export function useStars() {
  const [starredSites, setStarredSites] = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => {
    fetchStars()
      .then(setStarredSites)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const starredIds = new Set(starredSites.map(s => s.id));

  const toggleStar = useCallback(async (site) => {
    const wasStarred = starredIds.has(site.id);

    // Optimistic update
    if (wasStarred) {
      setStarredSites(prev => prev.filter(s => s.id !== site.id));
    } else {
      setStarredSites(prev => [...prev, site]);
    }

    try {
      if (wasStarred) {
        await removeStar(site.id);
        toast('Unstarred');
      } else {
        await addStar(site.id);
        toast('★ Starred');
      }
    } catch (err) {
      // Roll back on failure
      if (wasStarred) {
        setStarredSites(prev => [...prev, site]);
      } else {
        setStarredSites(prev => prev.filter(s => s.id !== site.id));
      }
      toast.error('Failed to update star');
    }
  }, [starredIds]);

  const reorder = useCallback(async (newOrder) => {
    setStarredSites(newOrder); // optimistic
    try {
      await reorderStars(newOrder.map(s => s.id));
    } catch {
      refresh(); // roll back
    }
  }, [refresh]);

  return { starredSites, starredIds, toggleStar, reorder, loading };
}
