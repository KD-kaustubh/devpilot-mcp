import { useEffect, useState } from "react";
import type { Access, CallStatus, ToolCall } from "./types";

export const ACCESS: Record<Access, { label: string; short: string; dot: string; badge: string }> = {
  read: {
    label: "Read-only",
    short: "read",
    dot: "bg-emerald-400",
    badge: "text-emerald-600 dark:text-emerald-300 bg-emerald-500/10 border-emerald-500/25",
  },
  write: {
    label: "Writes files",
    short: "writes",
    dot: "bg-amber-400",
    badge: "text-amber-600 dark:text-amber-300 bg-amber-500/10 border-amber-500/25",
  },
  execute: {
    label: "Executes code",
    short: "runs code",
    dot: "bg-rose-400",
    badge: "text-rose-600 dark:text-rose-300 bg-rose-500/10 border-rose-500/25",
  },
};

export const STATUS_LABEL: Record<CallStatus, string> = {
  running: "Running",
  awaiting: "Waiting for approval",
  ok: "Done",
  error: "Failed",
  blocked: "Blocked by DevPilot security",
  denied: "Denied by you",
};

/** Human names for tools, used on cards and quick actions. */
export const TOOL_LABEL: Record<string, string> = {
  list_directory: "List directory",
  read_file: "Read file",
  search_files: "Search files",
  search_code: "Search code",
  analyze_repository: "Analyze repository",
  git_status: "Git status",
  git_log: "Git log",
  git_diff: "Git diff",
  git_branch: "Git branches",
  github_repository: "GitHub repository",
  github_issues: "GitHub issues",
  github_pull_requests: "GitHub pull requests",
  investigate_repository: "Investigate",
  apply_patch: "Apply patch",
  revert_patch: "Revert patch",
  get_test_commands: "Detect tests",
  run_tests: "Run tests",
  validate_repository: "Validate repository",
};

export interface QuickAction {
  label: string;
  short: string;
  tool: string;
  args: Record<string, unknown>;
  hint: string;
}

export const QUICK_ACTIONS: QuickAction[] = [
  { label: "Analyze repository", short: "Analyze", tool: "analyze_repository", args: {}, hint: "Languages, structure, manifests, tests" },
  { label: "Git status", short: "Git status", tool: "git_status", args: {}, hint: "Branch and uncommitted changes" },
  { label: "Recent commits", short: "Commits", tool: "git_log", args: { limit: 10 }, hint: "The last 10 commits" },
  { label: "Detect tests", short: "Detect tests", tool: "get_test_commands", args: {}, hint: "Framework and command, runs nothing" },
  { label: "Validate repository", short: "Validate", tool: "validate_repository", args: {}, hint: "Checks without running code" },
  { label: "Run tests", short: "Run tests", tool: "run_tests", args: {}, hint: "Executes test code (asks first)" },
];

export const SUGGESTIONS = [
  "Explain the structure of this repository",
  "Where is the main entry point?",
  "What changed in the last few commits?",
  "How is this project tested?",
];

export function formatDuration(ms: number | undefined): string {
  if (ms === undefined) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} m ${Math.round((ms % 60_000) / 1000)} s`;
}

/** One-line summary of a call's arguments, e.g. `query: "auth"`. */
export function argsPreview(call: Pick<ToolCall, "arguments">): string {
  const args = call.arguments;
  if (!args || Object.keys(args).length === 0) return "";
  return Object.entries(args)
    .map(([key, value]) => {
      const shown = typeof value === "string" ? `"${value.length > 48 ? value.slice(0, 48) + "…" : value}"` : JSON.stringify(value);
      return `${key}: ${shown}`;
    })
    .join(" · ");
}

/** Re-render on an interval while `active` (live timers). */
export function useNow(active: boolean, intervalMs = 100): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

export function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => {
    try {
      return localStorage.getItem("devpilot-theme") !== "light";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("devpilot-theme", dark ? "dark" : "light");
    } catch {
      /* storage can be unavailable; the theme still works for this visit */
    }
  }, [dark]);
  return [dark, () => setDark((d) => !d)];
}
