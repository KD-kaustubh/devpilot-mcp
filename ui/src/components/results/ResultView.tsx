import { Ban, CircleX, ShieldAlert } from "lucide-react";
import type { ToolCall } from "../../types";
import { CodeBlock } from "../CodeViews";
import { BranchView, GitDiffView, GitLogView, GitStatusView, PatchView, RevertView } from "./GitViews";
import { GithubItemsView, GithubRepoView, TestCommandsView, TestRunView, ValidateView } from "./GithubTestViews";
import { AnalyzeView, DirectoryView, FileView, InvestigateView, SearchView } from "./RepoViews";

type Data = Record<string, any>;
type View = React.ComponentType<{ data: Data; compact: boolean; callId: string }>;

const VIEWS: Record<string, View> = {
  analyze_repository: AnalyzeView,
  list_directory: DirectoryView,
  read_file: FileView,
  search_code: SearchView,
  search_files: SearchView,
  investigate_repository: InvestigateView,
  git_status: GitStatusView,
  git_log: GitLogView,
  git_diff: GitDiffView,
  git_branch: BranchView,
  github_repository: GithubRepoView,
  github_issues: GithubItemsView,
  github_pull_requests: GithubItemsView,
  get_test_commands: TestCommandsView,
  run_tests: TestRunView,
  validate_repository: ValidateView,
  revert_patch: RevertView,
};

/** Strips the SDK's "Error executing tool x: " prefix so the message reads naturally. */
export function cleanError(text: string | undefined): string {
  return (text ?? "").replace(/^Error executing tool \w+: /, "");
}

export function ResultView({ call, compact = false }: { call: ToolCall; compact?: boolean }) {
  if (call.status === "running" || call.status === "awaiting") {
    return <div className="shimmer h-1 w-full rounded-full" />;
  }
  if (call.status === "blocked") {
    return (
      <div className="flex items-start gap-2.5 rounded-xl border border-rose-500/30 bg-rose-500/8 px-3 py-2.5 text-[12.5px]">
        <ShieldAlert size={16} className="mt-0.5 shrink-0 text-rose-500" />
        <div>
          <div className="font-semibold text-rose-600 dark:text-rose-300">Blocked by DevPilot security</div>
          <div className="text-[var(--text-muted)]">{cleanError(call.text)}</div>
        </div>
      </div>
    );
  }
  if (call.status === "denied") {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-[var(--border)] px-3 py-2.5 text-[12.5px] text-[var(--text-muted)]">
        <Ban size={15} /> You denied this action, so nothing ran.
      </div>
    );
  }
  if (call.status === "error" || !call.structured) {
    return (
      <div className="flex items-start gap-2.5 rounded-xl border border-rose-500/25 bg-rose-500/5 px-3 py-2.5 text-[12.5px]">
        <CircleX size={16} className="mt-0.5 shrink-0 text-rose-500" />
        <div className="text-[var(--text-muted)]">{cleanError(call.text) || "The tool returned no result."}</div>
      </div>
    );
  }
  if (call.name === "apply_patch") {
    const patch = typeof call.arguments?.patch === "string" ? call.arguments.patch : undefined;
    return <PatchView data={call.structured} compact={compact} callId={call.id} patch={patch} />;
  }
  const View = VIEWS[call.name];
  if (View) return <View data={call.structured as Data} compact={compact} callId={call.id} />;
  return <CodeBlock code={JSON.stringify(call.structured, null, 2)} language="json" maxHeight={compact ? "240px" : undefined} />;
}
