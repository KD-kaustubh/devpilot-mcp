import { motion } from "framer-motion";
import { ChevronDown, GitCompareArrows, Info, RefreshCw, Undo2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { QueryResult, ToolCall } from "../types";
import { DiffView } from "./CodeViews";
import { GitDiffView } from "./results/GitViews";
import { ChangeTag, Chip, Empty, FileLink, Section, relativeTime } from "./results/parts";
import { SlideOver } from "./SlideOver";

export interface AppliedChange { call: ToolCall; changeId: string; undone: boolean }

export function ChangesPanel({ open, onClose, changes, busy, onUndo, query }: {
  open: boolean;
  onClose: () => void;
  changes: AppliedChange[];
  busy: boolean;
  onUndo: (changeId: string) => void;
  query: (name: string, args: Record<string, unknown>) => Promise<QueryResult>;
}) {
  const [diff, setDiff] = useState<QueryResult | null>(null);

  const loadDiff = useCallback(async () => {
    setDiff(null);
    setDiff(await query("git_diff", {}));
  }, [query]);

  // Reload the diff whenever a change is applied or undone (not only when the count changes).
  const changeKey = changes.map((c) => `${c.changeId}:${c.undone}`).join(",");
  useEffect(() => {
    if (open) loadDiff();
  }, [open, loadDiff, changeKey]);

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      icon={<GitCompareArrows size={18} className="text-amber-400" />}
      title={<div><div className="font-semibold">Changes</div><div className="text-[11.5px] text-[var(--text-faint)]">Patches applied in this session, and the current Git diff</div></div>}
    >
      <div className="space-y-6 p-5">
        <Section title={`Applied by DevPilot · ${changes.length}`}>
          {changes.length === 0 ? (
            <Empty>No changes applied yet. When the AI (or you, with Ctrl+K → Apply patch) applies a patch, it appears here with an Undo button.</Empty>
          ) : (
            <div className="space-y-3">
              {changes.map((c) => <ChangeCard key={c.changeId} change={c} busy={busy} onUndo={onUndo} />)}
            </div>
          )}
          <div className="flex gap-2 text-[11.5px] text-[var(--text-faint)]">
            <Info size={13} className="mt-0.5 shrink-0" />
            Undo works while this devpilot-ui keeps running; the change history is kept in memory only. After a restart, use Git to undo.
          </div>
        </Section>

        <Section
          title="Current Git diff (unstaged)"
          aside={<button onClick={loadDiff} className="inline-flex items-center gap-1 hover:text-[var(--text)]"><RefreshCw size={12} /> refresh</button>}
        >
          {!diff ? <div className="shimmer h-1 rounded-full" />
            : diff.status === "ok" && diff.structured ? <GitDiffView data={diff.structured} compact={false} callId="diff" />
            : <Empty>{diff.text.replace(/^Error executing tool \w+: /, "")}</Empty>}
        </Section>
      </div>
    </SlideOver>
  );
}

function ChangeCard({ change, busy, onUndo }: { change: AppliedChange; busy: boolean; onUndo: (id: string) => void }) {
  const [showDiff, setShowDiff] = useState(false);
  const s = change.call.structured as Record<string, any>;
  const patch = typeof change.call.arguments?.patch === "string" ? change.call.arguments.patch : "";
  return (
    <motion.div layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
      className={`overflow-hidden rounded-xl border ${change.undone ? "border-[var(--border)] opacity-60" : "border-amber-400/30"}`}>
      <div className="flex flex-wrap items-center gap-2 px-3.5 py-2.5">
        <Chip tone={change.undone ? undefined : "warn"}>{change.undone ? "undone" : "applied"}</Chip>
        <Chip mono>{change.changeId}</Chip>
        <span className="font-mono text-[12px]"><span className="text-emerald-500">+{s.additions}</span> <span className="text-rose-500">−{s.deletions}</span></span>
        <span className="text-[11.5px] text-[var(--text-faint)]">{relativeTime(s.applied_at)}</span>
        {!change.undone && (
          <button
            onClick={() => onUndo(change.changeId)}
            disabled={busy}
            title={busy ? "Wait for the current request to finish" : "Restore the files exactly as they were"}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] px-2.5 py-1 text-[12px] font-medium transition hover:border-amber-400/50 hover:bg-amber-500/10 disabled:opacity-40"
          >
            <Undo2 size={13} /> Undo
          </button>
        )}
      </div>
      <div className="space-y-1 border-t border-[var(--border)] px-3.5 py-2.5">
        {(s.files_changed ?? []).map((f: Record<string, any>) => (
          <div key={f.path} className="flex items-center gap-2 text-[12.5px]">
            <ChangeTag change={f.change} /><FileLink path={f.path} />
            <span className="ml-auto font-mono text-[11px]"><span className="text-emerald-500">+{f.additions}</span> <span className="text-rose-500">−{f.deletions}</span></span>
          </div>
        ))}
      </div>
      {patch && (
        <div className="border-t border-[var(--border)]">
          <button onClick={() => setShowDiff((v) => !v)} className="flex w-full items-center gap-1.5 px-3.5 py-2 text-[12px] text-[var(--text-muted)] hover:text-[var(--text)]">
            <motion.span animate={{ rotate: showDiff ? 180 : 0 }}><ChevronDown size={13} /></motion.span> {showDiff ? "Hide" : "Show"} patch
          </button>
          {showDiff && <div className="px-3.5 pb-3.5"><DiffView patch={patch} maxHeight="36vh" /></div>}
        </div>
      )}
    </motion.div>
  );
}
