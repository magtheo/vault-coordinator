import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getScheduleRange } from "../api";
import type { TodayEvent } from "../types";

function fmtTime(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

function eventDay(e: TodayEvent): string {
  if (!e.start) return "";
  return e.start.slice(0, 10);
}

export function CalendarView() {
  const now = new Date();
  const [cursor, setCursor] = useState({
    y: now.getFullYear(),
    m: now.getMonth(), // 0-based
  });
  const [selectedDay, setSelectedDay] = useState<string>(now.toISOString().slice(0, 10));

  const monthStart = `${cursor.y}-${String(cursor.m + 1).padStart(2, "0")}-01`;
  const lastDay = new Date(Date.UTC(cursor.y, cursor.m + 1, 0)).getUTCDate();
  const monthEnd = `${cursor.y}-${String(cursor.m + 1).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;

  const rangeQuery = useQuery({
    queryKey: ["schedule-range", monthStart],
    queryFn: () => getScheduleRange(monthStart, monthEnd),
  });

  const events = rangeQuery.data?.events ?? [];

  const byDay = useMemo(() => {
    const map = new Map<string, TodayEvent[]>();
    for (const e of events) {
      const d = eventDay(e);
      if (!d) continue;
      const list = map.get(d) ?? [];
      list.push(e);
      map.set(d, list);
    }
    return map;
  }, [events]);

  // Calendar grid: Monday-first
  const firstDow = (new Date(Date.UTC(cursor.y, cursor.m, 1)).getUTCDay() + 6) % 7;
  const daysInMonth = lastDay;
  const cells: (string | null)[] = [
    ...Array.from({ length: firstDow }, () => null),
    ...Array.from(
      { length: daysInMonth },
      (_, i) => `${cursor.y}-${String(cursor.m + 1).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`,
    ),
  ];
  while (cells.length % 7 !== 0) cells.push(null);

  const monthLabel = new Date(Date.UTC(cursor.y, cursor.m, 1)).toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
  const todayStr = now.toISOString().slice(0, 10);
  const dayEvents = byDay.get(selectedDay) ?? [];

  function shiftMonth(delta: number) {
    setCursor((c) => {
      const d = new Date(Date.UTC(c.y, c.m + delta, 1));
      return { y: d.getUTCFullYear(), m: d.getUTCMonth() };
    });
  }

  return (
    <div>
      <div className="app-header">
        <h1>🗓️ Calendar</h1>
        <div className="cal-nav">
          <button className="btn-secondary btn-sm" onClick={() => shiftMonth(-1)}>‹</button>
          <span className="cal-month-label">{monthLabel}</span>
          <button className="btn-secondary btn-sm" onClick={() => shiftMonth(1)}>›</button>
        </div>
      </div>

      {rangeQuery.isPending && <div className="loading">Loading…</div>}
      {rangeQuery.error instanceof Error && (
        <div className="error-text">{rangeQuery.error.message}</div>
      )}

      <div className="cal-grid">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <div key={d} className="cal-dow">{d}</div>
        ))}
        {cells.map((day, i) =>
          day === null ? (
            <div key={`e${i}`} className="cal-cell empty" />
          ) : (
            <button
              key={day}
              className={`cal-cell ${day === todayStr ? "today" : ""} ${day === selectedDay ? "selected" : ""}`}
              onClick={() => setSelectedDay(day)}
            >
              <span className="cal-daynum">{Number(day.slice(8))}</span>
              {(byDay.get(day)?.length ?? 0) > 0 && (
                <span className="cal-dot">{byDay.get(day)!.length}</span>
              )}
            </button>
          ),
        )}
      </div>

      <div className="today-section">
        <div className="section-label">
          {new Date(selectedDay + "T00:00:00").toLocaleDateString("en-US", {
            weekday: "long",
            month: "short",
            day: "numeric",
          })}
        </div>
        {dayEvents.length === 0 && (
          <div className="empty-state">No events this day.</div>
        )}
        {dayEvents.map((event) => (
          <div key={event.uid} className="today-event">
            <div className="event-time">
              {fmtTime(event.start)} – {fmtTime(event.end)}
            </div>
            <div className="event-title">{event.summary}</div>
            {event.linked_alias && (
              <div className="event-link">🔗 {event.linked_title || event.linked_alias}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
