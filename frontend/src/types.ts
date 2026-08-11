// ─── Types matching coordinator API responses ─────────────────────────

export interface Task {
  id: string;
  alias: string;
  title: string;
  source: string;      // "vikunja" | "git"
  type: string;        // "vikunja_task" | "repo_task"
  last_synced: string;
  raw?: Record<string, unknown> | null;
}

export interface TasksResponse {
  tasks: Task[];
  sync_status: Record<string, {
    last_success: string | null;
    consecutive_failures: number;
  }>;
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
  vikunja?: { synced: number; errors: string[] };
  git?: { synced: number; errors: string[] };
}

// ─── View state ────────────────────────────────────────────────────────

export type View = "tasks" | "today";
