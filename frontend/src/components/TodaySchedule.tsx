import { useState, useEffect, useCallback } from "react";
import type { TodayResponse } from "../types";
import { getToday } from "../api";

export function TodaySchedule() {
  const [data, setData] = useState<TodayResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const resp = await getToday();
      setData(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load schedule");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const events = data?.events ?? [];
  const dateLabel = data
    ? new Date(data.date + "T00:00:00").toLocaleDateString("en-US", {
        weekday: "long",
        month: "short",
        day: "numeric",
      })
    : "";

  function formatTime(iso: string | null): string {
    if (!iso) return "";
    try {
      const dt = new Date(iso);
      return dt.toLocaleTimeString("en-US", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
    } catch {
      return iso;
    }
  }

  return (
    <div>
      <div className="app-header">
        <h1>📅 {dateLabel}</h1>
        <button className="sync-btn" onClick={load}>↻</button>
      </div>

      {loading && <div className="loading">Loading schedule…</div>}
      {error && <div className="error-text">{error}</div>}

      {!loading && !error && (
        <div>
          {events.length === 0 ? (
            <div className="empty-state">
              No time blocks scheduled today.
              <br />
              Go to Tasks to schedule work.
            </div>
          ) : (
            events.map((event) => (
              <div key={event.uid} className="today-event">
                <div className="event-time">
                  {formatTime(event.start)} – {formatTime(event.end)}
                </div>
                <div className="event-title">{event.summary}</div>
                {event.linked_alias && (
                  <div className="event-link">
                    🔗 {event.linked_title || event.linked_alias}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
