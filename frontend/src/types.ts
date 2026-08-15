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
}

// ─── Projects overview (slice 2) ───────────────────────────────────────

export interface ProjectTask {
  alias: string;
  title: string;
  kind: string;
}

export interface ProjectSession {
  host: string;
  name: string;
  windows: number;
  created: string;
}

export interface ProjectJob {
  host: string;
  name: string;
  state: string;
  since: string | null;
  command: string;
}

export interface ProjectOverview {
  name: string;
  source: string;
  host: string | null;
  open_tasks: number;
  done_tasks: number;
  tasks: ProjectTask[];
  sessions: ProjectSession[];
  jobs: ProjectJob[];
}

export interface ProjectsOverviewResponse {
  projects: ProjectOverview[];
}

// ─── View state ────────────────────────────────────────────────────────

export type View = "today" | "tasks" | "projects" | "machines";
