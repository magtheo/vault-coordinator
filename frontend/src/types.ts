// ─── Types matching coordinator enriched API responses ─────────────────

export interface Capabilities {
  edit: boolean;
  complete: boolean;
  reopen: boolean;
  schedule: boolean;
  set_deadline: boolean;
  set_priority: boolean;
  set_project: boolean;
  open_source: boolean;
}

export interface Provenance {
  repo_id: string | null;
  repo_name: string | null;
  branch: string | null;
  commit: string | null;
  dirty: boolean | null;
}

export interface Label {
  id: number;
  title: string;
}

export interface Task {
  id: string;
  ref: string;
  kind: string;          // "vikunja_task" | "repo_task"
  title: string;
  source: string;        // "vikunja" | "git"
  source_status: string | null;
  project_ref: string | null;
  freshness: string;     // "fresh" | "stale" | "error" | "unknown"
  scheduled: boolean;
  capabilities: Capabilities;
  priority: number | null;
  due_date: string | null;
  labels?: Label[] | null;
  is_favorite: boolean | null;
  description: string | null;
  provenance: Provenance | null;
  last_synced: string;
  raw?: Record<string, unknown> | null;
}

export interface SyncStatusEntry {
  last_success: string | null;
  consecutive_failures: number;
  last_error: string | null;
  freshness: string;
}

export interface TasksResponse {
  tasks: Task[];
  sync_status: Record<string, SyncStatusEntry>;
}

export interface ScheduleResponse {
  event_uid: string;
  relationship_id: string;
  status: "created" | "already_exists";
}

export interface TodayEvent {
  uid: string;
  summary: string;
  start: string | null;
  end: string | null;
  vault_block_id: string;
  linked_alias: string | null;
  linked_title: string | null;
}

export interface TodayResponse {
  events: TodayEvent[];
  date: string;
}

export interface RangeResponse {
  from: string;
  to: string;
  events: TodayEvent[];
}

export interface RegistryProject {
  name: string;
  slug: string;
  kind: string;   // "vault" | "code"
  host?: string;
  path?: string;
}

export interface RegistryResponse {
  projects: RegistryProject[];
}

export interface SyncResponse {
  results: Record<string, { status: string; tasks?: number; error?: string }>;
}

export interface Mutation {
  id: string;
  entity_alias: string | null;
  operation: string;
  status: "pending" | "confirmed" | "failed";
  payload: string | null;
  result: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface MutationResponse {
  mutation: Mutation;
  task_alias?: string;
  task?: Record<string, unknown>;
}

export interface Project {
  ref: string;
  name: string;
  source: string;
  project_id: number | null;
  kind: string;
}

export interface ProjectsResponse {
  projects: Project[];
}

// ─── Machines (slice 1) ────────────────────────────────────────────────

export interface MachineJob {
  name: string;
  state: string;
  since: string | null;
  command: string;
}

export interface MachineSession {
  name: string;
  windows: number;
  created: string;
}

export interface Machine {
  name: string;
  host: string;
  timestamp: string | null;
  jobs: MachineJob[];
  sessions: MachineSession[];
  reachable: boolean;
  error: string | null;
}

export interface MachinesResponse {
  machines: Machine[];
  age_seconds?: number;
}

// ─── Projects overview (slice 2) ───────────────────────────────────────

export interface ProjectTask {
  alias: string;
  title: string;
  kind: string;
  due_date?: string | null;
  overdue?: boolean;
}

export interface ProjectSession {
  host: string;
  name: string;
  windows: number;
  panes?: PaneInfo[];
}

export interface ProjectJob {
  host: string;
  name: string;
  state: string;
  since: string | null;
  command: string;
  exit_code?: number | null;
}

export interface ProjectOverview {
  name: string;
  source: string;
  host: string | null;
  git?: GitInfo | null;
  open_tasks: number;
  done_tasks: number;
  overdue_tasks: number;
  tasks: ProjectTask[];
  sessions: ProjectSession[];
  jobs: ProjectJob[];
  commands: { host: string; key: string }[];
  attention: Attention;
}

export interface ProjectsOverviewResponse {
  projects: ProjectOverview[];
}

// ─── Actionable overview additions ─────────────────────────────────────

export interface GitInfo {
  project?: string;
  path: string;
  branch?: string;
  dirty?: boolean;
  commit_age_min?: number | null;
  last_subject?: string;
  unpushed?: number;
  error?: string;
}

export interface PaneInfo {
  window: string;
  window_name: string;
  command: string;
}

export interface Attention {
  failed_jobs: number;
  overdue_tasks: number;
  host_down: boolean;
  score: number;
}

export interface Alert {
  type: string;
  severity: "high" | "medium";
  message: string;
  machine?: string;
  job?: string;
  alias?: string;
}

export interface AttentionResponse {
  alerts: Alert[];
}

export interface AiSummaryResponse {
  summary: string;
  generated_at: number;
  model: string;
  cached: boolean;
}

export interface JobLogResponse {
  machine: string;
  job: string;
  log: string;
}

// ─── View state ────────────────────────────────────────────────────────

export type View = "today" | "tasks" | "calendar" | "projects";
