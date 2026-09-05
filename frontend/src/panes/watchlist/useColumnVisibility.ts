import { useEffect, useMemo, useState } from "react";
import { ALL_COLUMNS, ALL_KEYS } from "./columns";
import type { SortKey } from "./sort";

const COLUMN_STORAGE_KEY = "tf.watchlist.columns";

export function useColumnVisibility() {
  const [visibleKeys, setVisibleKeys] = useState<Set<SortKey>>(() => {
    try {
      const raw = localStorage.getItem(COLUMN_STORAGE_KEY);
      if (raw) {
        const arr = JSON.parse(raw) as string[];
        const set = new Set(arr.filter((k): k is SortKey => ALL_KEYS.includes(k as SortKey)));
        // New columns default to visible even if prefs were saved before they existed.
        for (const key of ALL_KEYS) {
          if (!arr.includes(key)) set.add(key);
        }
        set.add("symbol"); // Symbol is always shown.
        return set;
      }
    } catch {
      /* ignore malformed storage */
    }
    return new Set(ALL_KEYS);
  });

  useEffect(() => {
    try {
      localStorage.setItem(COLUMN_STORAGE_KEY, JSON.stringify([...visibleKeys]));
    } catch {
      /* ignore */
    }
  }, [visibleKeys]);

  const visibleColumns = useMemo(
    () => ALL_COLUMNS.filter((c) => visibleKeys.has(c.key)),
    [visibleKeys],
  );

  function toggleColumn(key: SortKey) {
    if (key === "symbol") return;
    setVisibleKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      next.add("symbol");
      return next;
    });
  }

  return { visibleKeys, visibleColumns, toggleColumn };
}
