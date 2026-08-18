import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Task } from "../types";
import { completeTask, reopenTask } from "../api";

const GRACE_MS = 5000;

interface PendingEntry {
  task: Task;
  until: number;
}

/**
 * Complete-with-undo: fires the completion immediately (never lost),
 * keeps the row visible (checked/dimmed) for a grace period, exposes an
 * undo (reopen). After the grace the task lists refresh and the row
 * disappears. Tap the checked circle or the floating pill to undo.
 */
export function useCompleteWithUndo(onToast: (msg: string, ok: boolean) => void) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<Map<string, PendingEntry>>(new Map());
  const [now, setNow] = useState(Date.now());
  const pendingRef = useRef(pending);
  pendingRef.current = pending;

  const hasPending = pending.size > 0;

  useEffect(() => {
    if (!hasPending) return;
    const id = window.setInterval(() => {
      const t = Date.now();
      setNow(t);
      let expired = false;
      for (const e of pendingRef.current.values()) {
        if (t >= e.until) {
          expired = true;
          break;
        }
      }
      if (expired) {
        setPending((prev) => {
          const next = new Map(prev);
          for (const [ref, e] of [...next]) {
            if (t >= e.until) next.delete(ref);
          }
          return next;
        });
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        queryClient.invalidateQueries({ queryKey: ["projects-overview"] });
      }
    }, 400);
    return () => window.clearInterval(id);
  }, [hasPending, queryClient]);

  const complete = useCallback(
    (task: Task) => {
      setPending((prev) => new Map(prev).set(task.ref, { task, until: Date.now() + GRACE_MS }));
      setNow(Date.now());
      completeTask(task.ref).catch((e) => {
        setPending((prev) => {
          const next = new Map(prev);
          next.delete(task.ref);
          return next;
        });
        onToast(e instanceof Error ? e.message : "Complete failed", false);
      });
    },
    [onToast],
  );

  const undo = useCallback(
    (ref: string) => {
      setPending((prev) => {
        const next = new Map(prev);
        next.delete(ref);
        return next;
      });
      reopenTask(ref)
        .then(() => onToast("Undone", true))
        .catch((e) => {
          onToast(e instanceof Error ? e.message : "Undo failed", false);
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
        });
    },
    [onToast, queryClient],
  );

  const isChecked = useCallback((ref: string) => pending.has(ref), [pending]);

  let latest: PendingEntry | null = null;
  for (const e of pending.values()) {
    if (!latest || e.until > latest.until) latest = e;
  }

  const toggle = useCallback(
    (task: Task) => {
      if (pendingRef.current.has(task.ref)) undo(task.ref);
      else complete(task);
    },
    [complete, undo],
  );

  return { complete, undo, toggle, isChecked, latest, now };
}

export function UndoBanner({
  entry,
  now,
  onUndo,
}: {
  entry: PendingEntry | null;
  now: number;
  onUndo: () => void;
}) {
  if (!entry) return null;
  const secs = Math.max(0, Math.ceil((entry.until - now) / 1000));
  return (
    <div className="undo-banner" role="status">
      <span className="undo-text">✓ {entry.task.title}</span>
      <button className="undo-btn" onClick={onUndo}>
        Undo · {secs}s
      </button>
    </div>
  );
}
