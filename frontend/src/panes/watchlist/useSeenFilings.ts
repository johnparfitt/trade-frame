import { useCallback, useState } from "react";

// Persisted "acknowledged" ids. Generic on purpose (id-based) so other
// attention signals can reuse it later; today it tracks SEC filings keyed by
// their EDGAR URL, which uniquely identifies a filing. Mirrors how the
// watchlist's column-visibility prefs persist to localStorage.
const STORAGE_KEY = "tf.seenFilings";

function loadStored(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed.map(String)) : new Set();
  } catch {
    return new Set();
  }
}

function saveStored(key: string, ids: Set<string>): void {
  try {
    localStorage.setItem(key, JSON.stringify([...ids]));
  } catch {
    // Ignore quota/availability errors — this state is a nicety, not critical.
  }
}

function loadSeen(): Set<string> {
  return loadStored(STORAGE_KEY);
}

function saveSeen(seen: Set<string>): void {
  saveStored(STORAGE_KEY, seen);
}

export function useSeenFilings() {
  const [seen, setSeen] = useState<Set<string>>(loadSeen);

  const isSeen = useCallback((id: string) => seen.has(id), [seen]);

  const markSeen = useCallback((id: string) => {
    setSeen((prev) => {
      if (prev.has(id)) return prev;
      const next = new Set(prev);
      next.add(id);
      saveSeen(next);
      return next;
    });
  }, []);

  const markSeenMany = useCallback((ids: string[]) => {
    setSeen((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const id of ids) {
        if (!id || next.has(id)) continue;
        next.add(id);
        changed = true;
      }
      if (!changed) return prev;
      saveSeen(next);
      return next;
    });
  }, []);

  return { isSeen, markSeen, markSeenMany };
}

const DISMISSED_KEY = "tf.dismissedFilings";

export function useDismissedFilings() {
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadStored(DISMISSED_KEY));

  const isDismissed = useCallback((id: string) => dismissed.has(id), [dismissed]);

  const dismissMany = useCallback((ids: string[]) => {
    setDismissed((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const id of ids) {
        if (!id || next.has(id)) continue;
        next.add(id);
        changed = true;
      }
      if (!changed) return prev;
      saveStored(DISMISSED_KEY, next);
      return next;
    });
  }, []);

  return { isDismissed, dismissMany };
}
