import { useEffect, useRef, useState } from "react";
import { ALL_COLUMNS } from "./columns";
import type { SortKey } from "./sort";

export default function ColumnPicker({
  visible,
  onToggle,
}: {
  visible: Set<SortKey>;
  onToggle: (key: SortKey) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="rounded-md border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
      >
        Columns ▾
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 w-48 rounded-md border border-slate-700 bg-slate-900 p-2 shadow-lg">
          {ALL_COLUMNS.map((col) => {
            const isSymbol = col.key === "symbol";
            return (
              <label
                key={col.key}
                className={`flex items-center gap-2 rounded px-2 py-1 text-sm ${
                  isSymbol ? "text-slate-500" : "cursor-pointer text-slate-200 hover:bg-slate-800"
                }`}
              >
                <input
                  type="checkbox"
                  checked={visible.has(col.key)}
                  disabled={isSymbol}
                  onChange={() => onToggle(col.key)}
                  className="accent-sky-600"
                />
                {col.label}
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
