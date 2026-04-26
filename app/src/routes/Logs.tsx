import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect, useMemo, useState } from "react";

const MAX_LINES = 5000;

type LogRow = {
  raw: string;
  level: string;
  message: string;
};

function parseLine(raw: string): LogRow {
  try {
    const parsed = JSON.parse(raw) as { level?: unknown; message?: unknown; msg?: unknown };
    const level = typeof parsed.level === "string" ? parsed.level.toUpperCase() : "INFO";
    const messageValue = parsed.message ?? parsed.msg;
    return {
      raw,
      level,
      message: typeof messageValue === "string" ? messageValue : raw
    };
  } catch {
    return { raw, level: "INFO", message: raw };
  }
}

function appendBounded(current: string[], next: string[]) {
  return [...current, ...next].slice(-MAX_LINES);
}

function levelClass(level: string) {
  if (level === "DEBUG") return "log-row debug";
  if (level === "WARNING" || level === "WARN") return "log-row warn";
  if (level === "ERROR") return "log-row error";
  return "log-row";
}

export default function Logs() {
  const [lines, setLines] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [copyState, setCopyState] = useState("");

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;

    async function loadInitialLines() {
      try {
        const rows = await invoke<string[]>("logs_tail", { lines: 200 });
        if (!disposed) {
          setLines(rows);
        }
      } catch {
        if (!disposed) {
          setLines([]);
        }
      }
    }

    async function subscribe() {
      unlisten = await listen<string>("logs-line", (event) => {
        setLines((current) => appendBounded(current, [event.payload]));
      });
    }

    void loadInitialLines();
    void subscribe();

    return () => {
      disposed = true;
      if (unlisten) {
        unlisten();
      }
    };
  }, []);

  const rows = useMemo(() => lines.map(parseLine), [lines]);
  const visibleRows = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((row) => row.raw.toLowerCase().includes(q));
  }, [filter, rows]);

  async function copyVisible() {
    await navigator.clipboard.writeText(visibleRows.map((row) => row.raw).join("\n"));
    setCopyState("Copied");
    window.setTimeout(() => setCopyState(""), 1200);
  }

  return (
    <section className="page">
      <h1>Logs</h1>
      <div className="logs-toolbar">
        <input
          aria-label="Filter logs"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter…"
        />
        <button type="button" onClick={() => void invoke("logs_open_in_finder")}>
          Show in Finder
        </button>
        <button type="button" onClick={() => void copyVisible()}>
          Copy visible
        </button>
      </div>
      {copyState ? <div className="notice">{copyState}</div> : null}
      <div className="logs-pane">
        {visibleRows.length === 0 ? (
          <div className="empty-state">No log entries yet. Run Now to generate logs.</div>
        ) : (
          visibleRows.map((row, index) => (
            <div key={`${index}-${row.raw.slice(0, 32)}`} className={levelClass(row.level)}>
              <strong>{row.level}</strong>
              <span>{row.message}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
