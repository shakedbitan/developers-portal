import { useState, useEffect } from 'react';
import { fetchMe } from '../api/index.js';

export function useAuth() {
  const [user, setUser]       = useState(null);   // { username, is_admin }
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchMe()
      .then(setUser)
      .catch(() => setUser({ username: '', is_admin: false }))
      .finally(() => setLoading(false));
  }, []);

  return { user, loading, isAdmin: user?.is_admin ?? false };
}
