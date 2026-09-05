import type { ComponentType } from "react";
import WatchlistPane from "./watchlist";

export interface Pane {
  path: string;
  label: string;
  Component: ComponentType;
}

/** Append-only registry: a new pane is a folder under panes/ plus one object here. */
export const PANES: Pane[] = [
  { path: "/watchlist", label: "Watchlist", Component: WatchlistPane },
];
