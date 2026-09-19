export type UserRole = "viewer" | "analyst" | "admin";
export type DashboardRole = "viewer" | "editor" | "owner";

export interface Principal {
  id: string;
  email: string;
  full_name: string | null;
  role: UserRole;
  tenant_id: string;
  scopes: { data_source_id: string; scope_key: string; scope_values: (string | number)[] }[];
}

export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  request_id?: string;
  [k: string]: unknown;
}

export interface ChartSpec {
  data: Record<string, unknown>[];
  layout: Record<string, unknown>;
}

export interface Suggestions {
  dataset: string;
  headline: string;
  scope_label: string;
  questions: string[];
}

export interface Session {
  id: string;
  data_source_id: string;
  title: string | null;
  created_at: string;
  last_active_at: string;
}

export interface TurnChart {
  id: string;
  position: number;
  title: string;
  chart_spec: ChartSpec;
  sql_text: string;
  sql_hash: string;
}

export interface GuardEvent {
  kind: string;
  verdict: string;
  reason: string;
  shadow_parser_verdict?: string | null;
}

export interface Turn {
  id: string;
  session_id: string;
  question: string;
  answer_markdown: string | null;
  status: "ok" | "blocked" | "error" | "timeout";
  error_code: string | null;
  sql_text: string | null;
  charts: TurnChart[];
  chart_order: number[];
  guard_events: GuardEvent[];
  model: string | null;
  prompt_version_id: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  duration_ms: number | null;
  stage_timings: Record<string, number> | null;
  trace_id: string;
  llm_calls: number | null;
  created_at: string;
}

export interface Chart {
  id: string;
  owner_id: string;
  data_source_id: string;
  title: string;
  description: string | null;
  question: string;
  sql_text: string;
  sql_hash: string;
  chart_spec: ChartSpec;
  params: Record<string, unknown>;
  source_turn_id: string | null;
  version: number;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChartSummary {
  id: string;
  title: string;
  description: string | null;
  question: string;
  sql_hash: string;
  version: number;
  is_archived: boolean;
  placement_count: number;
  created_at: string;
  updated_at: string;
}

export interface ChartUsage {
  chart_id: string;
  dashboard_count: number;
  placements: { dashboard_id: string; dashboard_name: string; owner_id: string; group_id: string; group_title: string; tile_id: string; title_override: string | null }[];
}

export interface Tile {
  id: string;
  dashboard_id: string;
  group_id: string;
  saved_chart_id: string;
  title: string;
  title_override: string | null;
  question: string;
  sql_text: string;
  sql_hash: string;
  chart_spec: ChartSpec;
  chart_version: number;
  position: number;
  x: number;
  y: number;
  w: number;
  h: number;
  overrides: Record<string, unknown>;
}

export interface Group {
  id: string;
  title: string;
  position: number;
  is_collapsed: boolean;
  tiles: Tile[];
}

export interface DashboardSummary {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  visibility: "private" | "shared" | "tenant";
  effective_role: DashboardRole;
  tile_count: number;
  updated_at: string;
}

export interface Dashboard extends Omit<DashboardSummary, "tile_count"> {
  is_archived: boolean;
  groups: Group[];
  created_at: string;
}

export interface TileRefresh {
  tile_id: string;
  status: "ok" | "blocked" | "invalid_query" | "error";
  columns: string[];
  rows: unknown[][];
  row_count: number | null;
  truncated: boolean;
  duration_ms: number;
  sql_hash: string | null;
  drifted: boolean;
  error_code: string | null;
  message: string | null;
  trace_id: string | null;
  llm_calls: number;
  chart_spec: ChartSpec | null;
  refreshed_at: string;
}

export interface DashboardRefresh {
  dashboard_id: string;
  tiles: TileRefresh[];
  llm_calls: number;
}

export interface Grant {
  principal_id: string;
  email: string | null;
  full_name: string | null;
  role: DashboardRole;
  granted_at: string;
}

export interface TenantUser {
  id: string;
  email: string;
  full_name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

export interface GroupingProposal {
  proposal_id: string;
  groups: { title: string; tile_ids: string[] }[];
  rationale: string;
  diff: { tile_id: string; title: string; from: string; to: string; changed: boolean }[];
}

export interface Span {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  start_ts: string | null;
  end_ts: string | null;
  duration_ms: number | null;
  status: string | null;
  attributes: Record<string, unknown>;
  children: Span[];
}

export interface Trace {
  trace_id: string;
  span_count: number;
  llm_calls: number;
  total_duration_ms: number | null;
  roots: Span[];
  turn: {
    id: string; question: string; status: string; error_code: string | null; model: string | null; prompt_version_id: string | null;
    input_tokens: number | null; output_tokens: number | null; cost_usd: number | null; duration_ms: number | null; stage_timings: Record<string, number> | null;
  } | null;
  guard_events: { kind: string; verdict: string; reason: string; offending_sql: string | null; shadow_parser_verdict: string | null; created_at: string }[];
}

export interface UsageRow {
  user_id: string;
  email: string | null;
  day: string;
  turn_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}
