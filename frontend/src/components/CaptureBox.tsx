import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Label, Task, TasksResponse } from "../types";
import { getLabels, createTask } from "../api";

interface Props {
  onToast: (msg: string, ok: boolean) => void;
  defaultLabelIds?: number[];
}

const LAST_LABELS_KEY = "vault_last_labels";

function parseTitle(raw: string): { title: string; labelTitles: string[] } {
  // "#tag rest of title" — leading tags are stripped from the title text
  const tokens = raw.trim().split(/\s+/);
  const labelTitles: string[] = [];
  let i = 0;
  while (i < tokens.length && tokens[i].startsWith("#") && tokens[i].length > 1) {
    labelTitles.push(tokens[i].slice(1).toLowerCase());
    i += 1;
  }
  return { title: tokens.slice(i).join(" "), labelTitles };
}

export function CaptureBox({ onToast, defaultLabelIds = [] }: Props) {
  const queryClient = useQueryClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedIds, setSelectedIds] = useState<number[]>(defaultLabelIds);
  const [acQuery, setAcQuery] = useState<string | null>(null);
  const [acIndex, setAcIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const labelsQuery = useQuery({ queryKey: ["labels"], queryFn: getLabels, staleTime: 60_000 });
  const labels: Label[] = labelsQuery.data ?? [];

  const selected = useMemo(
    () =>
      selectedIds
        .map((id) => labels.find((l) => l.id === id))
        .filter((l): l is Label => Boolean(l)),
    [selectedIds, labels],
  );

  const suggestions = useMemo(() => {
    if (acQuery == null) return [];
    const q = acQuery.toLowerCase();
    return labels.filter((l) => l.title.toLowerCase().includes(q)).slice(0, 6);
  }, [acQuery, labels]);

  function syncAcState(value: string) {
    // Track trailing "#partial" token being typed
    const m = value.match(/#([a-z0-9-]*)$/i);
    setAcQuery(m ? m[1].toLowerCase() : null);
    setAcIndex(0);
  }

  function toggleLabel(id: number) {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }

  function pickSuggestion(label: Label) {
    // Replace the trailing "#partial" token with nothing (label becomes a chip)
    setText((t) => t.replace(/#([a-z0-9-]*)$/i, "").replace(/\s+$/, " "));
    setSelectedIds((prev) => (prev.includes(label.id) ? prev : [...prev, label.id]));
    setAcQuery(null);
    inputRef.current?.focus();
  }

  async function submit() {
    const { title, labelTitles } = parseTitle(text);
    if (!title.trim() && labelTitles.length === 0) return;
    // Merge inline #tags (by title match) with chip-selected labels
    const ids = new Set(selectedIds);
    for (const lt of labelTitles) {
      const found = labels.find((l) => l.title.toLowerCase() === lt);
      if (found) ids.add(found.id);
    }
    const finalTitle = title.trim() || text.trim().replace(/#[a-z0-9-]+/gi, "").trim();
    if (!finalTitle) return;

    setBusy(true);
    try {
      const resp = await createTask({
        title: finalTitle,
        label_ids: ids.size ? [...ids] : undefined,
      });

      // Insert the created task into the ["tasks"] cache directly from
      // the create response — appearance is instant and never depends on
      // a refetch racing the service worker. Invalidation below reconciles.
      const raw = resp.task;
      if (raw && typeof (raw as Record<string, unknown>).id === "number") {
        const t = raw as Record<string, unknown>;
        const alias = `vikunja:local:task:${t.id}`;
        const enriched: Task = {
          id: alias,
          ref: alias,
          kind: "vikunja_task",
          title: String(t.title ?? finalTitle),
          source: "vikunja",
          source_status: "open",
          project_ref: null,
          freshness: "fresh",
          scheduled: false,
          capabilities: {
            edit: true,
            complete: true,
            reopen: true,
            schedule: true,
            set_deadline: true,
            set_priority: true,
            set_project: true,
            open_source: false,
          },
          priority: (t.priority as number) ?? null,
          due_date: (t.due_date as string) ?? null,
          labels: Array.isArray(t.labels)
            ? (t.labels as { id: number; title: string }[]).map((l) => ({
                id: l.id,
                title: l.title,
              }))
            : null,
          is_favorite: null,
          description: null,
          provenance: null,
          last_synced: new Date().toISOString(),
          raw: t,
        };
        queryClient.setQueryData<TasksResponse>(["tasks"], (old) => {
          if (!old) return old;
          if (old.tasks.some((x) => x.ref === alias)) return old;
          return { ...old, tasks: [enriched, ...old.tasks] };
        });
      }

      const last = [...ids];
      if (last.length) localStorage.setItem(LAST_LABELS_KEY, JSON.stringify(last));
      onToast("Task created", true);
      setText("");
      setSelectedIds([]);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    } catch (e) {
      onToast(e instanceof Error ? e.message : "Capture failed", false);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (acQuery != null && suggestions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAcIndex((i) => (i + 1) % suggestions.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setAcIndex((i) => (i - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && suggestions[acIndex])) {
        e.preventDefault();
        pickSuggestion(suggestions[acIndex]);
        return;
      }
      if (e.key === "Escape") {
        setAcQuery(null);
        return;
      }
    }
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="capture-box">
      <div className="capture-row">
        <input
          ref={inputRef}
          type="text"
          placeholder="Add a task…  #tag for labels"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            syncAcState(e.target.value);
          }}
          onKeyDown={onKeyDown}
          className="capture-input"
          autoFocus
        />
        <button className="btn-primary btn-sm" onClick={submit} disabled={busy}>
          {busy ? "…" : "Add"}
        </button>
      </div>

      {selected.length > 0 && (
        <div className="capture-chips">
          {selected.map((l) => (
            <button
              key={l.id}
              className="label-chip selected"
              onClick={() => toggleLabel(l.id)}
              title="Remove label"
            >
              #{l.title} ✕
            </button>
          ))}
        </div>
      )}

      {acQuery != null && suggestions.length > 0 && (
        <div className="autocomplete">
          {suggestions.map((l, i) => (
            <button
              key={l.id}
              className={`autocomplete-item ${i === acIndex ? "active" : ""}`}
              onMouseDown={(e) => {
                e.preventDefault();
                pickSuggestion(l);
              }}
              onMouseEnter={() => setAcIndex(i)}
            >
              #{l.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Session-default labels (last used), read once at mount by the parent. */
export function lastUsedLabelIds(): number[] {
  try {
    const raw = localStorage.getItem(LAST_LABELS_KEY);
    return raw ? (JSON.parse(raw) as number[]) : [];
  } catch {
    return [];
  }
}
