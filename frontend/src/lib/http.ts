/** Shared HTTP helpers for pane API clients. */

/** State of a manual background job (breadth refresh, report generate). */
export type JobState = "idle" | "running" | "error";

/**
 * Unwrap a fetch Response into JSON, throwing an Error with the backend's
 * `detail` message on non-2xx. Returns undefined for 204 No Content.
 */
export async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}
