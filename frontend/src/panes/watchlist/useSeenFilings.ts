import { useCallback, useState } from "react";

// Persisted "acknowledged" ids. Generic on purpose (id-based) so other
// attention signals can reuse it later; today it tracks SEC filings keyed by
// their EDGAR URL, which uniquely identifies a filing. Mirrors how the
// watchlist's column-visibility prefs persist to localStorage.
const STORAGE_KEY = "tf.seenFilings";

function loadSeen(): Set<string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? new Set(parsed.map(String)) : new Set();
  } catch {
    return new Set();
  }
}

function saveSeen(seen: Set<string>): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...seen]));
  } catch {
    // Ignore quota/availability errors — seen-state is a nicety, not critical.
  }
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

  return { isSeen, markSeen };
}
