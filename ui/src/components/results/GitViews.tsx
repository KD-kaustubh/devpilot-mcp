import { GitBranch, GitCommitHorizontal, Undo2 } from "lucide-react";
import { DiffView } from "../CodeViews";
import { ChangeTag, Chip, Empty, FileLink, More, Section, Stat, relativeTime, take, useResultActions } from "./parts";

type Data = Record<string, any>;
interface ViewProps { data: Data; compact: boolean; callId: string }

function FileRows({ items, kind }: { items: Data[]; kind?: string }) {
  return (
    <div className="space-y-0.5">
      {items.map((f) => (
        <div key={(f.path ?? f) + (f.change ?? kind ?? "")} className="flex items-center gap-2 text-[12.5px]">
          <ChangeTag change={f.change ?? f.action ?? kind ?? ""} />
          <FileLink path={f.path ?? f} />
          {f.original_path && <span className="truncate text-[11px] text-[var(--text-faint)]">from {f.original_path}</span>}
          {(f.additions != null || f.deletions != null) && (
            <span className="ml-auto shrink-0 font-mono text-[11px]">
              <span className="text-emerald-500">+{f.additions ?? 0}</span> <span className="text-rose-500">−{f.deletions ?? 0}</span>
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

export function GitStatusView({ data: s, compact }: ViewProps) {
  const limit = compact ? 6 : 1000;
  const groups: [string, Data[], string?][] = [
    ["Staged", s.staged ?? []], ["Unstaged", s.unstaged ?? []],
    ["Untracked", (s.untracked ?? []).map((p: string) => ({ path: p })), "untracked"],
    ["Conflicted", (s.conflicted ?? []).map((p: string) => ({ path: p })), "conflicted"],
  ];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip mono><GitBranch size={12} /> {s.branch ?? "detached HEAD"}</Chip>
        {s.head_commit && <Chip mono>{String(s.head_commit).slice(0, 7)}</Chip>}
        {s.upstream && <Chip>{s.upstream} · ↑{s.ahead ?? 0} ↓{s.behind ?? 0}</Chip>}
        <Chip tone={s.clean ? "good" : "warn"}>{s.clean ? "working tree clean" : "uncommitted changes"}</Chip>
      </div>
      {groups.filter(([, items]) => items.length > 0).map(([title, items, kind]) => (
        <Section key={title} title={`${title} · ${items.length}`}>
          <FileRows items={items.slice(0, limit)} kind={kind} />
          {items.length > limit && <div className="text-[11.5px] text-[var(--text-faint)]">+ {items.length - limit} more</div>}
        </Section>
      ))}
    </div>
  );
}

export function GitLogView({ data: s, compact, callId }: ViewProps) {
  const [commits, more] = take(s.commits as Data[], compact, 5);
  if (commits.length === 0) return <Empty>No commits yet.</Empty>;
  return (
    <div className="space-y-1">
      <ol className="relative space-y-2 before:absolute before:bottom-1 before:left-[6.5px] before:top-1 before:w-px before:bg-[var(--border)]">
        {commits.map((c) => (
          <li key={c.hash} className="relative flex gap-3 pl-0">
            <GitCommitHorizontal size={14} className="relative z-10 mt-0.5 shrink-0 bg-[var(--panel-solid)] text-cyan-500" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium">{c.subject}</div>
              <div className="text-[11.5px] text-[var(--text-faint)]">
                <span className="font-mono text-cyan-600 dark:text-cyan-400">{c.short_hash}</span> · {c.author_name} · {relativeTime(c.date)}
              </div>
              {!compact && c.body && <div className="mt-1 whitespace-pre-wrap text-[12px] text-[var(--text-muted)]">{c.body}</div>}
            </div>
          </li>
        ))}
      </ol>
      <More hidden={more} callId={callId} />
      {!compact && s.has_more && <div className="text-[11.5px] text-[var(--text-faint)]">Older history exists.</div>}
    </div>
  );
}

export function GitDiffView({ data: s, compact }: ViewProps) {
  if (!s.files_changed) return <Empty>No {s.staged ? "staged" : "unstaged"} changes{s.path && s.path !== "." ? ` in ${s.path}` : ""}.</Empty>;
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-3 gap-2">
        <Stat label={s.staged ? "files staged" : "files changed"} value={s.files_changed} />
        <Stat label="additions" value={`+${s.additions}`} tone="good" />
        <Stat label="deletions" value={`−${s.deletions}`} tone="bad" />
      </div>
      <FileRows items={(s.files ?? []).slice(0, compact ? 6 : 1000)} />
      {s.diff && <DiffView patch={s.diff} maxHeight={compact ? "220px" : "60vh"} />}
      {s.truncated && <div className="text-[11.5px] text-amber-600 dark:text-amber-400">The diff text was cut at 60,000 bytes; narrow it with a path.</div>}
    </div>
  );
}

export function BranchView({ data: s }: ViewProps) {
  return (
    <div className="space-y-1">
      {(s.branches ?? []).map((b: Data) => (
        <div key={b.name} className="flex items-center gap-2 text-[12.5px]">
          <GitBranch size={13} className={b.current ? "text-emerald-500" : "text-[var(--text-faint)]"} />
          <span className={`font-mono ${b.current ? "font-semibold" : ""}`}>{b.name}</span>
          {b.current && <Chip tone="good">current</Chip>}
          <span className="ml-auto font-mono text-[11px] text-[var(--text-faint)]">{b.commit}{b.upstream ? ` → ${b.upstream}` : ""}</span>
        </div>
      ))}
    </div>
  );
}

export function PatchView({ data: s, compact, patch }: ViewProps & { patch?: string }) {
  const { undo } = useResultActions();
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="good">applied</Chip>
        <Chip mono>{s.change_id}</Chip>
        <span className="font-mono text-[12px]"><span className="text-emerald-500">+{s.additions}</span> <span className="text-rose-500">−{s.deletions}</span></span>
        {s.reversible && (
          <button
            onClick={() => undo(s.change_id)}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] px-2.5 py-1 text-[12px] font-medium transition hover:border-amber-400/50 hover:bg-amber-500/10"
          >
            <Undo2 size={13} /> Undo
          </button>
        )}
      </div>
      <FileRows items={s.files_changed ?? []} />
      {!compact && patch && <DiffView patch={patch} />}
    </div>
  );
}

export function RevertView({ data: s }: ViewProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2"><Chip tone="good">undone</Chip><Chip mono>{s.change_id}</Chip></div>
      <FileRows items={s.files_restored ?? []} />
    </div>
  );
}
