import { useEffect, useRef } from 'react';

export function usePolling(
  callback: () => void | Promise<void>,
  intervalMs: number,
  enabled = true,
  immediate = true,
) {
  const savedCallback = useRef(callback);

  // Keep the latest callback in a ref — writing inside an effect (not during
  // render) per the react-hooks refs rule.
  useEffect(() => {
    savedCallback.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let running = false;
    let timer: number | undefined;

    const schedule = () => {
      if (!cancelled) timer = window.setTimeout(run, intervalMs);
    };
    const run = async () => {
      if (cancelled || running) return;
      if (document.hidden) {
        schedule();
        return;
      }
      running = true;
      try {
        await savedCallback.current();
      } catch (error) {
        // A transient network failure must not stop all later refreshes or
        // surface as an unhandled rejected promise.
        console.error('Polling callback failed', error);
      } finally {
        running = false;
        schedule();
      }
    };
    const onVisibilityChange = () => {
      if (!document.hidden && !running) {
        if (timer !== undefined) window.clearTimeout(timer);
        void run();
      }
    };

    document.addEventListener('visibilitychange', onVisibilityChange);
    if (immediate) void run();
    else schedule();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [intervalMs, enabled, immediate]);
}
