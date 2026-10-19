// Response shapes of the FastAPI backend (see /docs on the running API).

export type Role = "analyst" | "viewer" | "admin";
export type Capability = "catalog" | "preview" | "query" | "refresh";

export interface User {
  username: string;
  display_name: string;
  role: Role;
  capabilities: Capability[];
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
}

export interface Column {
  name: string;
  type: string;
  description?: string;
}

export interface Dataset {
  name: string;
  kind: "fact" | "dimension" | "metric" | "quality";
  description: string;
  grain: string;
  primary_key: string[];
  columns: Column[];
  rows: number;
  filters: { date: string[]; facility: boolean };
}

export interface Catalog {
  build: { build_id: string; built_at: string };
  datasets: Dataset[];
}

export type Cell = string | number | boolean | null | Cell[];

export interface RowsPage {
  dataset: string;
  build_id: string;
  columns: Column[];
  rows: Cell[][];
  page: { limit: number; offset: number; total: number; has_more: boolean };
}

export interface QueryResult {
  build_id: string;
  tables: string[];
  columns: Column[];
  rows: Cell[][];
  row_count: number;
  offset: number;
  truncated: boolean;
  next_offset: number | null;
  ordered: boolean;
  elapsed_ms: number;
  cached: boolean;
}

export type JobStatus = "running" | "succeeded" | "partial" | "failed" | "interrupted";

export interface RefreshJob {
  job_id: string;
  status: JobStatus;
  step: "extract" | "model" | "done";
  full: boolean;
  requested_by: string;
  started_at: string;
  finished_at: string | null;
  extraction_run_id: string | null;
  datasets: Record<
    string,
    { status: string; rows_fetched: number; inserted: number; updated: number; watermark: string | null }
  >;
  build_id: string | null;
  dq_issues: Record<string, number>;
  error: string | null;
}
