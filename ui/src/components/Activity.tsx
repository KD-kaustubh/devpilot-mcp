import { AnimatePresence, motion } from "framer-motion";
import { Activity as ActivityIcon, Ban, Bot, CircleX, Hand, ShieldAlert, Trash2, User } from "lucide-react";
import { ACCESS, STATUS_LABEL, TOOL_LABEL, argsPreview, formatDuration, useNow } from "../lib";
import type { CallStatus, ToolCall } from "../types";

/** Animated state icon shared by timeline cards and chat pills. */
export function StatusIcon({ status, size = 18 }: { status: CallStatus; size?: number }) {
  if (status === "running") {
    return (
      <span className="relative grid place-items-center" style={{ width: size, height: size }}>
        <span className="absolute inset-0 animate-ping rounded-full bg-indigo-400/40" />
        <span
          className="relative rounded-full border-2 border-indigo-400/30 border-t-cyan-300 animate-spin"
          style={{ width: size, height: size }}
        />
      </span>
    );
  }
  if (status === "awaiting") {
    return (
      <motion.span animate={{ scale: [1, 1.15, 1] }} transition={{ repeat: Infinity, duration: 1.4 }} className="text-amber-400">
        <Hand size={size} />
      </motion.span>
    );
  }
  if (status === "ok") {
    return (
      <span className="grid place-items-center rounded-full bg-emerald-500/15 text-emerald-400" style={{ width: size, height: size }}>
        <svg viewBox="0 0 24 24" width={size * 0.7} height={size * 0.7} fill="none" stroke="currentColor" strokeWidth={3.2} strokeLinecap="round" strokeLinejoin="round">
          <motion.path d="M5 12.5l4.5 4.5L19 7.5" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.4, ease: "easeOut" }} />
        </svg>
      </span>
    );
  }
  if (status === "blocked") return <ShieldAlert size={size} className="text-rose-400" />;
  if (status === "denied") return <Ban size={size} className="text-slate-400" />;
  return <CircleX size={size} className="text-rose-400" />;
}

function ToolCard({ call, onOpen }: { call: ToolCall; onOpen: (id: string) => void }) {
  const live = call.status === "running" || call.status === "awaiting";
  const now = useNow(live);
  const elapsed = live ? now - call.startedAt : call.durationMs;
  const shake = call.status === "blocked" || call.status === "error";
  const preview = argsPreview(call);

  return (
    <motion.li
      layout
      initial={{ opacity: 0, x: 24, scale: 0.97 }}
      animate={shake ? { opacity: 1, x: [0, -6, 6, -4, 4, 0], scale: 1 } : { opacity: 1, x: 0, scale: 1 }}
      exit={{ opacity: 0, x: 24 }}
      transition={{ duration: shake ? 0.45 : 0.3 }}
      className="relative pl-7"
    >
      <span className="absolute left-0 top-3.5">
        <StatusIcon status={call.status} />
      </span>
      <button
        onClick={() => onOpen(call.id)}
        className={`glass relative w-full overflow-hidden rounded-xl p-3 text-left transition hover:border-indigo-400/40 ${
          call.status === "blocked" ? "border-rose-500/35 bg-rose-500/5" : call.status === "awaiting" ? "border-amber-400/40" : ""}`}
      >
        <div className="flex items-center gap-2">
          <span className="font-mono text-[12.5px] font-semibold">{call.name}</span>
          <span className={`rounded-full border px-1.5 py-px text-[10px] ${ACCESS[call.access].badge}`}>{ACCESS[call.access].short}</span>
          <span className="ml-auto flex items-center gap-1 text-[10.5px] text-[var(--text-faint)]" title={call.source === "agent" ? "Requested by the AI" : "Requested by you"}>
            {call.source === "agent" ? <Bot size={12} /> : <User size={12} />}
            <span className="tabular-nums">{formatDuration(elapsed)}</span>
          </span>
        </div>
        <div className="mt-0.5 text-[11.5px] text-[var(--text-muted)]">
          {call.status === "ok" ? TOOL_LABEL[call.name] ?? call.name : STATUS_LABEL[call.status]}
        </div>
        {preview && <div className="mt-1 truncate font-mono text-[10.5px] text-[var(--text-faint)]">{preview}</div>}
        {call.status === "blocked" && (
          <div className="mt-1.5 line-clamp-2 text-[11px] text-rose-400">{call.text?.replace(/^Error executing tool \w+: /, "")}</div>
        )}
        {live && <div className="shimmer absolute inset-x-0 bottom-0 h-[2px]" />}
      </button>
    </motion.li>
  );
}

export function ActivityPanel({ calls, order, onOpen, onClear }: {
  calls: Record<string, ToolCall>; order: string[]; onOpen: (id: string) => void; onClear: () => void;
}) {
  const items = [...order].reverse().map((id) => calls[id]).filter(Boolean);
  const counts = items.reduce((acc, c) => ({ ...acc, [c.status]: (acc[c.status] ?? 0) + 1 }), {} as Record<string, number>);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 px-4 pb-3 pt-4">
        <ActivityIcon size={16} className="text-cyan-400" />
        <h2 className="font-semibold">Activity</h2>
        <span className="text-[12px] text-[var(--text-faint)]">{items.length ? `${items.length} call${items.length === 1 ? "" : "s"}` : ""}</span>
        {items.length > 0 && (
          <button onClick={onClear} title="Clear activity" className="ml-auto rounded-md p-1.5 text-[var(--text-faint)] hover:bg-[var(--panel-muted)] hover:text-[var(--text)]">
            <Trash2 size={14} />
          </button>
        )}
      </div>
      {items.length > 0 && (
        <div className="mx-4 mb-3 flex gap-3 text-[11px] text-[var(--text-muted)]">
          <span><b className="text-emerald-400">{counts.ok ?? 0}</b> ok</span>
          <span><b className="text-rose-400">{(counts.error ?? 0) + (counts.blocked ?? 0)}</b> failed/blocked</span>
          <span><b className="text-cyan-400">{(counts.running ?? 0) + (counts.awaiting ?? 0)}</b> active</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 scroll-slim">
        {items.length === 0 ? (
          <div className="mt-10 grid place-items-center text-center text-[12.5px] text-[var(--text-faint)]">
            <div className="mb-3 grid h-12 w-12 place-items-center rounded-2xl border border-dashed border-[var(--border)]">
              <ActivityIcon size={20} />
            </div>
            Every tool call DevPilot makes<br />appears here, live.
          </div>
        ) : (
          <ul className="relative space-y-2.5 before:absolute before:bottom-2 before:left-[8.5px] before:top-2 before:w-px before:bg-[var(--border)]">
            <AnimatePresence initial={false}>
              {items.map((call) => <ToolCard key={call.id} call={call} onOpen={onOpen} />)}
            </AnimatePresence>
          </ul>
        )}
      </div>
    </div>
  );
}
