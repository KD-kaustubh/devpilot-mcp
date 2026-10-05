import { FileCode2 } from "lucide-react";
import { createContext, useContext } from "react";

/** Actions result cards can trigger, provided once by App. */
export interface ResultActions {
  openFile: (path: string, line?: number) => void;
  openDetails: (callId: string) => void;
  undo: (changeId: string) => void;
}

export const ResultActionsContext = createContext<ResultActions>({
  openFile: () => {},
  openDetails: () => {},
  undo: () => {},
});

export const useResultActions = () => useContext(ResultActionsContext);

export function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <h4 className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">{title}</h4>
        {aside && <span className="ml-auto text-[11px] text-[var(--text-faint)]">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "good" | "bad" | "warn" }) {
  const color = tone === "good" ? "text-emerald-500 dark:text-emerald-400" : tone === "bad" ? "text-rose-500 dark:text-rose-400"
    : tone === "warn" ? "text-amber-500 dark:text-amber-400" : "";
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--panel-muted)]/60 px-3 py-2">
      <div className={`text-lg font-semibold tabular-nums leading-tight ${color}`}>{value}</div>
      <div className="text-[11px] text-[var(--text-faint)]">{label}</div>
    </div>
  );
}

export function Chip({ children, tone, mono, title }: { children: React.ReactNode; tone?: "good" | "bad" | "warn" | "info"; mono?: boolean; title?: string }) {
  const color = tone === "good" ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300"
    : tone === "bad" ? "border-rose-500/25 bg-rose-500/10 text-rose-600 dark:text-rose-300"
    : tone === "warn" ? "border-amber-500/25 bg-amber-500/10 text-amber-600 dark:text-amber-300"
    : tone === "info" ? "border-indigo-500/25 bg-indigo-500/10 text-indigo-600 dark:text-indigo-300"
    : "border-[var(--border)] bg-[var(--panel-muted)]/60 text-[var(--text-muted)]";
  return (
    <span title={title} className={`inline-flex max-w-full items-center gap-1 truncate rounded-full border px-2 py-0.5 text-[11px] ${mono ? "font-mono" : ""} ${color}`}>
      {children}
    </span>
  );
}

export function FileLink({ path, line, label }: { path: string; line?: number | null; label?: string }) {
  const { openFile } = useResultActions();
  return (
    <button
      onClick={() => openFile(path, line ?? undefined)}
      title={`Open ${path}${line ? `:${line}` : ""}`}
      className="inline-flex max-w-full items-center gap-1 truncate font-mono text-[12px] text-indigo-600 hover:text-cyan-600 hover:underline dark:text-indigo-300 dark:hover:text-cyan-300"
    >
      <FileCode2 size={12} className="shrink-0 opacity-70" />
      <span className="truncate">{label ?? path}{line ? `:${line}` : ""}</span>
    </button>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rounded-xl border border-dashed border-[var(--border)] px-3 py-2.5 text-[12.5px] text-[var(--text-faint)]">{children}</div>;
}

export function More({ hidden, callId }: { hidden: number; callId: string }) {
  const { openDetails } = useResultActions();
  if (hidden <= 0) return null;
  return (
    <button onClick={() => openDetails(callId)} className="text-[12px] text-indigo-600 hover:underline dark:text-indigo-300">
      + {hidden} more · show all
    </button>
  );
}

const LANG_COLORS = ["#818cf8", "#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa", "#fb923c"];

/** Stacked bar of file counts per language, with a legend. */
export function LanguageBar({ languages, max = 6 }: { languages: Record<string, number>; max?: number }) {
  const entries = Object.entries(languages);
  const total = entries.reduce((sum, [, n]) => sum + n, 0) || 1;
  const top = entries.slice(0, max);
  const rest = entries.slice(max).reduce((sum, [, n]) => sum + n, 0);
  const parts = rest ? [...top, ["Other", rest] as [string, number]] : top;
  return (
    <div className="space-y-2">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-[var(--panel-muted)]">
        {parts.map(([name, n], i) => (
          <div key={name} title={`${name}: ${n}`} style={{ width: `${(n / total) * 100}%`, background: LANG_COLORS[i % LANG_COLORS.length] }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11.5px] text-[var(--text-muted)]">
        {parts.map(([name, n], i) => (
          <span key={name} className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ background: LANG_COLORS[i % LANG_COLORS.length] }} />
            {name} <span className="tabular-nums text-[var(--text-faint)]">{Math.round((n / total) * 100)}%</span>
          </span>
        ))}
      </div>
    </div>
  );
}

const CHANGE_TONE: Record<string, string> = {
  added: "text-emerald-500", created: "text-emerald-500", untracked: "text-emerald-500", restored: "text-emerald-500",
  modified: "text-amber-500", type_changed: "text-amber-500", renamed: "text-cyan-500", copied: "text-cyan-500",
  deleted: "text-rose-500", removed: "text-rose-500", unmerged: "text-rose-500", conflicted: "text-rose-500", recreated: "text-emerald-500",
};

export function ChangeTag({ change }: { change: string }) {
  return <span className={`w-16 shrink-0 text-[11px] font-medium ${CHANGE_TONE[change] ?? "text-[var(--text-faint)]"}`}>{change.replace("_", " ")}</span>;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(seconds)) return "";
  const units: [number, string][] = [[31_536_000, "y"], [2_592_000, "mo"], [86_400, "d"], [3_600, "h"], [60, "m"]];
  for (const [size, unit] of units) if (seconds >= size) return `${Math.floor(seconds / size)}${unit} ago`;
  return "just now";
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Show the first `limit` items in compact mode, all of them otherwise. */
export function take<T>(items: T[] | null | undefined, compact: boolean, limit: number): [T[], number] {
  const list = items ?? [];
  return compact ? [list.slice(0, limit), Math.max(0, list.length - limit)] : [list, 0];
}
