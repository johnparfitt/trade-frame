import type { ComponentType } from "react";
import BreadthPane from "./breadth";
import ReportPane from "./report";
import WatchlistPane from "./watchlist";

export interface Pane {
  path: string;
  label: string;
  Component: ComponentType;
}

/** Append-only registry: a new pane is a folder under panes/ plus one object here. */
export const PANES: Pane[] = [
  { path: "/watchlist", label: "Watchlist", Component: WatchlistPane },
  { path: "/breadth", label: "Breadth", Component: BreadthPane },
  { path: "/report", label: "Report", Component: ReportPane },
];
