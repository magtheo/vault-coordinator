import type {
  TasksResponse,
  ScheduleResponse,
  TodayResponse,
  RangeResponse,
  RegistryResponse,
  SyncResponse,
  MutationResponse,
  ProjectsResponse,
  ProjectsOverviewResponse,
  MachinesResponse,
  AttentionResponse,
  AiSummaryResponse,
  JobLogResponse,
  Label,
} from "./types";

const API = "/api";
const TOKEN_KEY = "vault_token";

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function ensureToken(): boolean {
  if (localStorage.getItem(TOKEN_KEY)) return true;
  const t = window.prompt("Vault access token");
  if (t) {
    localStorage.setItem(TOKEN_KEY, t);
    return true;
  }
  return false;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const doFetch = () =>
    fetch(url, {
      ...init,
      headers: { ...authHeaders(), ...(init?.headers ?? {}) },
    });
  let resp = await doFetch();
  if (resp.status === 401 && !url.includes("retry")) {
    // token missing/rejected → prompt once and retry
    localStorage.removeItem(TOKEN_KEY);
    if (ensureToken()) resp = await doFetch();
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch {
      // ignore parse error
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

function jsonBody(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function uuid(): string {
  return crypto.randomUUID();
}

// ─── Read endpoints ────────────────────────────────────────────────────

export function getTasks(source?: string, scheduled?: boolean): Promise<TasksResponse> {
  const params = new URLSearchParams();
  if (source) params.set("source", source);
  if (scheduled !== undefined) params.set("scheduled", String(scheduled));
  const qs = params.toString();
  return fetchJson<TasksResponse>(`${API}/tasks${qs ? `?${qs}` : ""}`);
}

export function getToday(): Promise<TodayResponse> {
  return fetchJson<TodayResponse>(`${API}/schedule/today`);
}

export function getScheduleRange(from: string, to: string): Promise<RangeResponse> {
  return fetchJson<RangeResponse>(
    `${API}/schedule/range?from=${from}&to=${to}`,
  );
}

export function getRegistry(): Promise<RegistryResponse> {
  return fetchJson<RegistryResponse>(`${API}/projects/registry`);
}

export function getLabels(): Promise<Label[]> {
  return fetchJson<Label[]>(`${API}/labels`);
}

export function appendScratchpad(body: {
  project: string | null;
  heading: string;
  body: string;
}): Promise<{ committed: boolean; commit_message: string }> {
  return fetchJson(`${API}/scratchpad`, jsonBody("POST", body));
}

export function getProjects(): Promise<ProjectsResponse> {
  return fetchJson<ProjectsResponse>(`${API}/projects`);
}

// ─── Write endpoints — Vikunja mutations ───────────────────────────────

export function createTask(opts: {
  title: string;
  project_id?: number;
  priority?: number;
  due_date?: string;
  description?: string;
  label_ids?: number[];
}): Promise<MutationResponse> {
  return fetchJson<MutationResponse>(
    `${API}/tasks`,
    jsonBody("POST", { request_id: uuid(), ...opts }),
  );
}

export function attachLabel(alias: string, labelId: number): Promise<{ labels: Label[] }> {
  return fetchJson(
    `${API}/tasks/${encodeURIComponent(alias)}/labels/${labelId}`,
    { method: "PUT" },
  );
}

export function detachLabel(alias: string, labelId: number): Promise<{ labels: Label[] }> {
  return fetchJson(
    `${API}/tasks/${encodeURIComponent(alias)}/labels/${labelId}`,
    { method: "DELETE" },
  );
}

export function editTask(
  alias: string,
  fields: {
    title?: string;
    priority?: number;
    due_date?: string;
    project_id?: number;
    description?: string;
  },
): Promise<MutationResponse> {
  return fetchJson<MutationResponse>(
    `${API}/tasks/${encodeURIComponent(alias)}`,
    jsonBody("PATCH", { request_id: uuid(), ...fields }),
  );
}

export function completeTask(alias: string): Promise<MutationResponse> {
  return fetchJson<MutationResponse>(
    `${API}/tasks/${encodeURIComponent(alias)}/complete`,
    jsonBody("POST", { request_id: uuid() }),
  );
}

export function reopenTask(alias: string): Promise<MutationResponse> {
  return fetchJson<MutationResponse>(
    `${API}/tasks/${encodeURIComponent(alias)}/reopen`,
    jsonBody("POST", { request_id: uuid() }),
  );
}

// ─── Schedule endpoints ────────────────────────────────────────────────

export function scheduleTask(
  entityAlias: string,
  start: string,
  durationMinutes: number,
): Promise<ScheduleResponse> {
  return fetchJson<ScheduleResponse>(
    `${API}/schedule`,
    jsonBody("POST", {
      request_id: uuid(),
      entity_alias: entityAlias,
      start,
      duration_minutes: durationMinutes,
    }),
  );
}

export function deleteBlock(eventUid: string): Promise<unknown> {
  return fetchJson(`${API}/schedule/${encodeURIComponent(eventUid)}`, { method: "DELETE" });
}

// ─── Sync ──────────────────────────────────────────────────────────────

export function triggerSync(): Promise<SyncResponse> {
  return fetchJson<SyncResponse>(`${API}/sync`, { method: "POST" });
}

// ─── Machines endpoints ────────────────────────────────────────────────

export function getMachines(): Promise<MachinesResponse> {
  return fetchJson<MachinesResponse>(`${API}/machines`);
}

export function refreshMachines(): Promise<MachinesResponse> {
  return fetchJson<MachinesResponse>(`${API}/machines/refresh`, { method: "POST" });
}

export function stopMachineJob(machine: string, job: string): Promise<{ status: string }> {
  return fetchJson<{ status: string }>(
    `${API}/machines/${encodeURIComponent(machine)}/jobs/${encodeURIComponent(job)}/stop`,
    { method: "POST" },
  );
}

export function getProjectsOverview(): Promise<ProjectsOverviewResponse> {
  return fetchJson<ProjectsOverviewResponse>(`${API}/projects/overview`);
}

// ─── Attention + AI + logs ─────────────────────────────────────────────

export function getAttention(): Promise<AttentionResponse> {
  return fetchJson<AttentionResponse>(`${API}/attention`);
}

export function getAiSummary(force = false): Promise<AiSummaryResponse> {
  return fetchJson<AiSummaryResponse>(`${API}/ai/summary`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
}

export function getJobLog(machine: string, job: string, lines = 50): Promise<JobLogResponse> {
  return fetchJson<JobLogResponse>(
    `${API}/machines/${encodeURIComponent(machine)}/jobs/${encodeURIComponent(job)}/log?lines=${lines}`,
  );
}

export function runProjectCommand(
  machine: string,
  project: string,
  key: string,
): Promise<{ status: string }> {
  return fetchJson<{ status: string }>(
    `${API}/machines/${encodeURIComponent(machine)}/projects/${encodeURIComponent(project)}/commands/${encodeURIComponent(key)}/run`,
    { method: "POST" },
  );
}
