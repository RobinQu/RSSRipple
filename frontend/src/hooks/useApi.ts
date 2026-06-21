import { useState, useCallback } from 'react';
import type { APIResponse } from '../types';

export function useApi<T>(apiCall: (...args: unknown[]) => Promise<APIResponse<T>>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const execute = useCallback(async (...args: unknown[]) => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiCall(...args);
      if (res.success) {
        setData(res.data);
        return res;
      } else {
        setError(res.error?.message || 'Unknown error');
        return res;
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Network error';
      setError(msg);
      throw e;
    } finally {
      setLoading(false);
    }
  }, [apiCall]);

  return { data, loading, error, execute, setData };
}
