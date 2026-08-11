import type {
  TasksResponse,
  ScheduleResponse,
  TodayResponse,
  SyncResponse,
} from "./types";

const API = "/api";

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
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

export function getTasks(source?: string): Promise<TasksResponse> {
  const params = source ? `?source=${source}` : "";
  return fetchJson<TasksResponse>(`${API}/tasks${params}`);
}

export function getToday(): Promise<TodayResponse> {
  return fetchJson<TodayResponse>(`${API}/schedule/today`);
}

export function scheduleTask(
  entityAlias: string,
  start: string,
  durationMinutes: number,
): Promise<ScheduleResponse> {
  const requestId = crypto.randomUUID();
  return fetchJson<ScheduleResponse>(`${API}/schedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: requestId,
      entity_alias: entityAlias,
      start,
      duration_minutes: durationMinutes,
    }),
  });
}

export function triggerSync(): Promise<SyncResponse> {
  return fetchJson<SyncResponse>(`${API}/sync`, { method: "POST" });
}
