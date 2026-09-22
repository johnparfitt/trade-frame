import { handle, type JobState } from "../../lib/http";

export interface BreadthDay {
  session_date: string;
  universe_size: number;
  up_4: number;
  down_4: number;
  ratio_5d: number | null;
  ratio_10d: number | null;
  up_25_q: number | null;
  down_25_q: number | null;
  up_50_m: number | null;
  down_50_m: number | null;
  ma_stack: number | null;
  computed_at: string;
}

export interface BreadthStatus {
  state: JobState;
  progress: { done: number; total: number };
  as_of: string | null;
  detail: string | null;
  has_api_key: boolean;
}

export async function getBreadth(): Promise<BreadthDay[]> {
  return handle(await fetch("/api/breadth"));
}

export async function getBreadthStatus(): Promise<BreadthStatus> {
  return handle(await fetch("/api/breadth/status"));
}

export async function refreshBreadth(): Promise<BreadthStatus> {
  return handle(
    await fetch("/api/breadth/refresh", {
      method: "POST",
    }),
  );
}
