import { handle, type JobState } from "../../lib/http";

export interface DailyReport {
  session_date: string;
  generated_at: string;
  model: string;
  analysis: string;
}

export interface ReportStatus {
  state: JobState;
  detail: string | null;
  as_of: string | null;
  has_uw_key: boolean;
  has_llm_key: boolean;
}

export async function getReports(): Promise<DailyReport[]> {
  return handle(await fetch("/api/report"));
}

export async function getReportStatus(): Promise<ReportStatus> {
  return handle(await fetch("/api/report/status"));
}

export async function generateReport(): Promise<ReportStatus> {
  return handle(
    await fetch("/api/report/generate", {
      method: "POST",
    }),
  );
}
