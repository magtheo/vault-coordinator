import { useState } from "react";
import type { Task } from "../types";
import { scheduleTask } from "../api";

interface Props {
  task: Task;
  onClose: () => void;
  onScheduled: (msg: string, ok: boolean) => void;
}

const DURATIONS = [15, 25, 45, 60, 90];

type StartOption = "now" | "+15" | "+30" | "+1h" | "custom";

export function ScheduleSheet({ task, onClose, onScheduled }: Props) {
  const [startOption, setStartOption] = useState<StartOption>("now");
  const [customTime, setCustomTime] = useState("");
  const [duration, setDuration] = useState(25);
  const [submitting, setSubmitting] = useState(false);

  function computeStart(): string {
    const now = new Date();
    switch (startOption) {
      case "now":
        now.setMinutes(now.getMinutes(), 0, 0);
        break;
      case "+15":
        now.setMinutes(now.getMinutes() + 15, 0, 0);
        break;
      case "+30":
        now.setMinutes(now.getMinutes() + 30, 0, 0);
        break;
      case "+1h":
        now.setHours(now.getHours() + 1, 0, 0);
        break;
      case "custom":
        if (!customTime) return "";
        const [h, m] = customTime.split(":").map(Number);
        now.setHours(h, m, 0, 0);
        break;
    }
    // Format as local ISO without timezone
    const y = now.getFullYear();
    const mo = String(now.getMonth() + 1).padStart(2, "0");
    const d = String(now.getDate()).padStart(2, "0");
    const h = String(now.getHours()).padStart(2, "0");
    const mi = String(now.getMinutes()).padStart(2, "0");
    return `${y}-${mo}-${d}T${h}:${mi}:00`;
  }

  const startStr = computeStart();

  function formatRange(): string {
    if (!startStr) return "Pick a time";
    const [datePart, timePart] = startStr.split("T");
    const [h, m] = timePart.split(":");
    const startMins = parseInt(h) * 60 + parseInt(m);
    const endMins = startMins + duration;
    const eh = Math.floor(endMins / 60) % 24;
    const em = endMins % 60;
    return `${h}:${m} – ${String(eh).padStart(2, "0")}:${String(em).padStart(2, "0")}`;
  }

  const handleSchedule = async () => {
    if (!startStr) return;
    setSubmitting(true);
    try {
      const resp = await scheduleTask(task.ref, startStr, duration);
      onScheduled(`✅ Scheduled: ${formatRange()}`, true);
      onClose();
    } catch (e) {
      onScheduled(
        e instanceof Error ? `❌ ${e.message}` : "❌ Failed to schedule",
        false,
      );
    } finally {
      setSubmitting(false);
    }
  };

  const startOptions: { label: string; value: StartOption }[] = [
    { label: "Now", value: "now" },
    { label: "+15 min", value: "+15" },
    { label: "+30 min", value: "+30" },
    { label: "+1 hour", value: "+1h" },
    { label: "Pick time", value: "custom" },
  ];

  return (
    <>
      <div className="sheet-overlay" onClick={onClose} />
      <div className="sheet">
        <div className="sheet-handle" />
        <div className="sheet-title">{task.title}</div>
        <div className="sheet-subtitle">{task.ref}</div>

        <div className="section-label">Start</div>
        <div className="option-grid">
          {startOptions.map((opt) => (
            <button
              key={opt.value}
              className={`option-btn ${startOption === opt.value ? "selected" : ""}`}
              onClick={() => setStartOption(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {startOption === "custom" && (
          <input
            type="time"
            value={customTime}
            onChange={(e) => setCustomTime(e.target.value)}
            style={{
              width: "100%",
              padding: "12px",
              background: "var(--bg)",
              border: "2px solid var(--border)",
              borderRadius: "var(--radius)",
              color: "var(--text)",
              fontSize: "1rem",
              marginBottom: "12px",
            }}
          />
        )}

        <div className="section-label">Duration (minutes)</div>
        <div className="option-grid">
          {DURATIONS.map((d) => (
            <button
              key={d}
              className={`option-btn ${duration === d ? "selected" : ""}`}
              onClick={() => setDuration(d)}
            >
              {d}
            </button>
          ))}
        </div>

        <div className="time-preview">{formatRange()}</div>

        <div className="sheet-actions">
          <button className="btn-secondary" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button
            className="btn-primary"
            onClick={handleSchedule}
            disabled={!startStr || submitting}
          >
            {submitting ? "Scheduling…" : "Schedule"}
          </button>
        </div>
      </div>
    </>
  );
}
