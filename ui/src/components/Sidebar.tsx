import { AnimatePresence, motion } from "framer-motion";
import {
  ChevronDown, ClipboardCheck, FlaskConical, FolderGit2, GitBranch, History, Moon, Play, ScanSearch, Sun, Zap,
} from "lucide-react";
import { useState } from "react";
import { ACCESS, QUICK_ACTIONS } from "../lib";
import type { Studio } from "../useStudio";
import type { Access } from "../types";

const ACTION_ICONS: Record<string, typeof ScanSearch> = {
  analyze_repository: ScanSearch,
  git_status: GitBranch,
  git_log: History,
  get_test_commands: FlaskConical,
  validate_repository: ClipboardCheck,
  run_tests: Play,
};

export function Logo() {
  return (
    <div className="flex items-center gap-3">
      <motion.div
        initial={{ rotate: -90, scale: 0.6, opacity: 0 }}
        animate={{ rotate: 0, scale: 1, opacity: 1 }}
        transition={{ type: "spring", stiffness: 180, damping: 14 }}
        className="relative grid h-9 w-9 place-items-center"
      >
        <div className="absolute inset-0 rotate-45 rounded-[10px] bg-gradient-to-br from-indigo-400 to-cyan-400 shadow-lg shadow-indigo-500/30" />
        <span className="relative font-mono text-sm font-bold text-slate-950">D</span>
      </motion.div>
      <div className="leading-tight">
        <div className="font-semibold tracking-tight">DevPilot <span className="text-gradient">Studio</span></div>
        <div className="text-[11px] text-[var(--text-faint)]">MCP repository copilot</div>
      </div>
    </div>
  );
}

export function Sidebar({ studio, dark, toggleTheme }: { studio: Studio; dark: boolean; toggleTheme: () => void }) {
  const { status, busy, calls, order } = studio;
  const running = new Set(order.map((id) => calls[id]).filter((c) => c?.status === "running").map((c) => c.name));

  return (
    <aside className="flex h-full min-h-0 flex-col gap-4 p-4">
      <div className="flex items-center justify-between">
        <Logo />
        <button
          onClick={toggleTheme}
          className="rounded-lg p-2 text-[var(--text-muted)] transition hover:bg-[var(--panel-muted)] hover:text-[var(--text)]"
          title={dark ? "Light theme" : "Dark theme"}
        >
          {dark ? <Sun size={16} /> : <Moon size={16} />}
        </button>
      </div>

      <WorkspaceCard studio={studio} />

      <section>
        <SectionTitle>Quick actions</SectionTitle>
        <div className="grid grid-cols-2 gap-2">
          {QUICK_ACTIONS.map((action, i) => {
            const Icon = ACTION_ICONS[action.tool] ?? Zap;
            const risky = action.tool === "run_tests";
            return (
              <motion.button
                key={action.tool}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 * i }}
                whileHover={{ y: -2 }}
                whileTap={{ scale: 0.97 }}
                disabled={busy || studio.connection !== "open"}
                onClick={() => studio.runTool(action.tool, action.args, action.label)}
                title={action.hint}
                className="group flex items-center gap-2 rounded-xl border border-[var(--border)] bg-[var(--panel-muted)]/60 px-2.5 py-2 text-left text-[12.5px] font-medium transition hover:border-indigo-400/40 hover:bg-indigo-500/10 disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Icon size={15} className={risky ? "text-rose-400" : "text-indigo-400 group-hover:text-cyan-400"} />
                <span className="truncate">{action.short}</span>
              </motion.button>
            );
          })}
        </div>
      </section>

      <section className="min-h-0 flex-1 overflow-y-auto scroll-slim pr-1">
        <SectionTitle>Tools {status ? `· ${status.tools.length}` : ""}</SectionTitle>
        {(["read", "write", "execute"] as Access[]).map((access) => (
          <ToolGroup
            key={access}
            access={access}
            tools={(status?.tools ?? []).filter((t) => t.access === access)}
            running={running}
          />
        ))}
      </section>

      <div className="text-[11px] text-[var(--text-faint)]">
        v{status?.version ?? "…"} · runs locally · <span className="font-mono">127.0.0.1</span>
      </div>
    </aside>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">{children}</div>;
}

function WorkspaceCard({ studio }: { studio: Studio }) {
  const { status, connection } = studio;
  const git = status?.git;
  const changes = git?.counts ? (git.counts.staged ?? 0) + (git.counts.unstaged ?? 0) + (git.counts.untracked ?? 0) : 0;
  return (
    <div className="glass relative overflow-hidden rounded-2xl p-3.5">
      <div className="pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-indigo-500/20 blur-2xl" />
      <div className="flex items-start gap-2.5">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500/25 to-cyan-500/20 text-indigo-300">
          <FolderGit2 size={18} />
        </div>
        <div className="min-w-0">
          <div className="truncate font-semibold" title={status?.workspace.path}>{status?.workspace.name ?? "Connecting…"}</div>
          <div className="truncate font-mono text-[11px] text-[var(--text-faint)]" title={status?.workspace.path}>
            {status?.workspace.path ?? ""}
          </div>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11.5px]">
        {git?.available ? (
          <>
            <span className="inline-flex items-center gap-1 rounded-full border border-[var(--border)] px-2 py-0.5 font-mono">
              <GitBranch size={12} /> {git.branch ?? "detached"}
            </span>
            <span className={`rounded-full border px-2 py-0.5 ${git.clean ? ACCESS.read.badge : ACCESS.write.badge}`}>
              {git.clean ? "clean" : `${changes} change${changes === 1 ? "" : "s"}`}
            </span>
          </>
        ) : (
          <span className="text-[var(--text-faint)]">{status ? "Not a Git repository root" : ""}</span>
        )}
      </div>
      <div className="mt-2.5 flex items-center gap-2 text-[11.5px] text-[var(--text-muted)]">
        <span className={`relative flex h-2 w-2`}>
          {status?.ai.configured && connection === "open" && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
          )}
          <span className={`relative inline-flex h-2 w-2 rounded-full ${
            connection !== "open" ? "bg-amber-400" : status?.ai.configured ? "bg-emerald-400" : "bg-slate-500"}`} />
        </span>
        {connection !== "open" ? "Connecting to DevPilot…"
          : status?.ai.configured ? <span>AI · <span className="font-mono">{status.ai.model}</span> via {status.ai.provider}</span>
          : "AI chat off · quick actions only"}
      </div>
    </div>
  );
}

function ToolGroup({ access, tools, running }: { access: Access; tools: { name: string; summary: string }[]; running: Set<string> }) {
  const [open, setOpen] = useState(access !== "read");
  const meta = ACCESS[access];
  return (
    <div className="mb-1.5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] text-[var(--text-muted)] transition hover:bg-[var(--panel-muted)]"
      >
        <span className={`h-2 w-2 rounded-full ${meta.dot}`} />
        <span className="font-medium text-[var(--text)]">{meta.label}</span>
        <span className="text-[var(--text-faint)]">{tools.length}</span>
        <motion.span animate={{ rotate: open ? 0 : -90 }} className="ml-auto">
          <ChevronDown size={14} />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.ul
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            {tools.map((tool) => (
              <li
                key={tool.name}
                title={tool.summary}
                className={`ml-4 flex items-center gap-2 rounded-md px-2 py-1 font-mono text-[11.5px] transition ${
                  running.has(tool.name) ? "bg-indigo-500/15 text-[var(--text)]" : "text-[var(--text-muted)]"}`}
              >
                {running.has(tool.name) ? (
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-cyan-400" />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full bg-[var(--border)]" />
                )}
                <span className="truncate">{tool.name}</span>
              </li>
            ))}
          </motion.ul>
        )}
      </AnimatePresence>
    </div>
  );
}
