import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";
import { STATUS_LABEL, TOOL_LABEL, formatDuration, useNow } from "../lib";
import type { ToolCall } from "../types";
import { StatusIcon } from "./Activity";

/** The most useful single argument of a call, shown next to its name (path, query, limit…). */
export function stepDetail(call: ToolCall): string {
  const a = call.arguments ?? {};
  const value = a.path ?? a.query ?? a.change_id ?? (a.limit !== undefined ? `last ${a.limit}` : undefined)
    ?? (a.state ? `${a.state}` : undefined) ?? (a.framework ? `${a.framework}` : undefined)
    ?? (call.name === "apply_patch" && typeof a.patch === "string" ? patchTargets(a.patch) : undefined);
  return value === undefined || value === "." ? "" : String(value);
}

function patchTargets(patch: string): string {
  const files = [...patch.matchAll(/^\+\+\+ (?:b\/)?(.+)$/gm)].map((m) => m[1]).filter((f) => f !== "/dev/null");
  return files.length > 1 ? `${files[0]} +${files.length - 1}` : files[0] ?? "";
}

function StepRow({ call, onOpen, index }: { call: ToolCall; onOpen: (id: string) => void; index: number }) {
  const live = call.status === "running" || call.status === "awaiting";
  const now = useNow(live);
  const elapsed = live ? now - call.startedAt : call.durationMs;
  const detail = stepDetail(call);
  return (
    <motion.button
      initial={{ opacity: 0, x: -8 }}
      animate={call.status === "blocked" ? { opacity: 1, x: [0, -4, 4, -2, 0] } : { opacity: 1, x: 0 }}
      transition={{ delay: index * 0.04, duration: 0.3 }}
      onClick={() => onOpen(call.id)}
      className="relative flex w-full items-center gap-2.5 overflow-hidden rounded-lg px-2 py-1.5 text-left text-[12.5px] transition hover:bg-indigo-500/8"
    >
      <StatusIcon status={call.status} size={15} />
      <span className="shrink-0 font-medium">{TOOL_LABEL[call.name] ?? call.name}</span>
      {detail && <span className="min-w-0 truncate font-mono text-[11.5px] text-[var(--text-faint)]">{detail}</span>}
      <span className={`ml-auto shrink-0 text-[11px] tabular-nums ${
        call.status === "blocked" ? "text-rose-500" : call.status === "awaiting" ? "text-amber-500" : "text-[var(--text-faint)]"}`}>
        {call.status === "blocked" || call.status === "awaiting" || call.status === "denied" ? STATUS_LABEL[call.status] : formatDuration(elapsed)}
      </span>
      {live && <span className="shimmer absolute inset-x-2 bottom-0 h-px" />}
    </motion.button>
  );
}

export function Steps({ calls, working, onOpen }: { calls: ToolCall[]; working: boolean; onOpen: (id: string) => void }) {
  const [open, setOpen] = useState(working);
  // Expanded while the answer is being worked on; folds itself away once the answer is complete.
  useEffect(() => setOpen(working), [working]);
  if (calls.length === 0) return null;

  const total = calls.reduce((sum, c) => sum + (c.durationMs ?? 0), 0);
  const blocked = calls.filter((c) => c.status === "blocked").length;
  const failed = calls.filter((c) => c.status === "error").length;
  const active = calls.some((c) => c.status === "running" || c.status === "awaiting");

  return (
    <div className={`mb-3 overflow-hidden rounded-xl border transition ${active ? "border-indigo-400/40 bg-indigo-500/5" : "border-[var(--border)] bg-[var(--panel)]"}`}>
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px]">
        {active ? (
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-indigo-400/30 border-t-cyan-400" />
        ) : (
          <span className="grid h-3.5 w-3.5 place-items-center rounded-full bg-gradient-to-br from-indigo-400 to-cyan-400" />
        )}
        <span className="font-medium">{active ? "Working…" : `Used ${calls.length} tool${calls.length === 1 ? "" : "s"}`}</span>
        <span className="text-[var(--text-faint)]">
          {!active && total > 0 && formatDuration(total)}
          {blocked > 0 && <span className="text-rose-500"> · {blocked} blocked</span>}
          {failed > 0 && <span className="text-rose-500"> · {failed} failed</span>}
        </span>
        <motion.span animate={{ rotate: open ? 180 : 0 }} className="ml-auto text-[var(--text-faint)]"><ChevronDown size={15} /></motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="border-t border-[var(--border)] px-1.5 py-1"
          >
            {calls.map((call, i) => <StepRow key={call.id} call={call} onOpen={onOpen} index={i} />)}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
