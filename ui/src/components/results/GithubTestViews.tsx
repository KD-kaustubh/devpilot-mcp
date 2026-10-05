import { CircleCheck, CircleDot, CircleMinus, CircleX, GitPullRequest, Star, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { CodeBlock } from "../CodeViews";
import { Chip, Empty, FileLink, More, Section, Stat, relativeTime, take } from "./parts";

type Data = Record<string, any>;
interface ViewProps { data: Data; compact: boolean; callId: string }

// --- GitHub -------------------------------------------------------------------

export function GithubRepoView({ data: s }: ViewProps) {
  return (
    <div className="space-y-3">
      <div>
        <a href={s.html_url} target="_blank" rel="noreferrer" className="text-[14px] font-semibold hover:underline">{s.full_name}</a>
        {s.description && <div className="mt-0.5 text-[12.5px] text-[var(--text-muted)]">{s.description}</div>}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="stars" value={<span className="inline-flex items-center gap-1"><Star size={14} className="text-amber-400" />{s.stargazers_count}</span>} />
        <Stat label="forks" value={s.forks_count} />
        <Stat label="open issues + PRs" value={s.open_issues_count} />
        <Stat label="watchers" value={s.subscribers_count} />
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Chip>{s.visibility}</Chip>
        {s.language && <Chip tone="info">{s.language}</Chip>}
        {s.license && <Chip>{s.license}</Chip>}
        <Chip mono>{s.default_branch}</Chip>
        {(s.topics ?? []).map((t: string) => <Chip key={t}>#{t}</Chip>)}
      </div>
      <div className="text-[11.5px] text-[var(--text-faint)]">
        Last push {relativeTime(s.pushed_at)} · API: {s.rate_limit?.remaining ?? "?"}/{s.rate_limit?.limit ?? "?"} requests left{s.authenticated ? "" : " (unauthenticated)"}
      </div>
    </div>
  );
}

export function GithubItemsView({ data: s, compact, callId }: ViewProps) {
  const isPulls = Array.isArray(s.pull_requests);
  const [items, more] = take((isPulls ? s.pull_requests : s.issues) as Data[], compact, 5);
  if (items.length === 0) return <Empty>No {s.state} {isPulls ? "pull requests" : "issues"} in {s.repository}.</Empty>;
  return (
    <div className="space-y-1.5">
      {items.map((i) => {
        const merged = isPulls && i.merged_at;
        const tone = merged ? "text-violet-500" : i.state === "open" ? "text-emerald-500" : "text-rose-500";
        return (
          <a key={i.number} href={i.html_url} target="_blank" rel="noreferrer"
            className="flex items-start gap-2.5 rounded-lg px-1.5 py-1 text-[12.5px] hover:bg-indigo-500/5">
            {isPulls ? <GitPullRequest size={15} className={`mt-0.5 shrink-0 ${tone}`} /> : <CircleDot size={15} className={`mt-0.5 shrink-0 ${tone}`} />}
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{i.title}</div>
              <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--text-faint)]">
                #{i.number} · {i.author ?? "ghost"} · {relativeTime(i.updated_at)}
                {isPulls && <span className="font-mono">{i.source_branch} → {i.target_branch}</span>}
                {i.draft && <Chip>draft</Chip>}
                {merged && <Chip tone="info">merged</Chip>}
                {(i.labels ?? []).slice(0, 4).map((l: string) => <Chip key={l}>{l}</Chip>)}
              </div>
            </div>
          </a>
        );
      })}
      <More hidden={more} callId={callId} />
    </div>
  );
}

// --- Testing ------------------------------------------------------------------

export function TestCommandsView({ data: s }: ViewProps) {
  const detected = (s.detected ?? []) as Data[];
  if (detected.length === 0) return <Empty>No supported test framework was detected (pytest or unittest).</Empty>;
  return (
    <div className="space-y-3">
      {detected.map((d) => (
        <div key={d.framework} className={`rounded-xl border p-3 ${d.framework === s.primary?.framework ? "border-indigo-400/40 bg-indigo-500/5" : "border-[var(--border)]"}`}>
          <div className="flex items-center gap-2">
            <span className="font-semibold">{d.framework}</span>
            {d.framework === s.primary?.framework && <Chip tone="info">primary</Chip>}
            <Chip tone={d.confidence === "high" ? "good" : d.confidence === "medium" ? "warn" : undefined}>{d.confidence} confidence</Chip>
            {d.runner_available === false && <Chip tone="bad">not installed</Chip>}
          </div>
          <div className="mt-1.5 font-mono text-[11.5px] text-[var(--text-muted)]">{(d.command ?? []).join(" ")}</div>
          <div className="mt-1.5 flex flex-wrap gap-1">{(d.evidence ?? []).map((e: string) => <Chip key={e}>{e}</Chip>)}</div>
        </div>
      ))}
      <div className="text-[11.5px] text-[var(--text-faint)]">
        {s.test_file_count} test files · Python {s.interpreter?.python_version ?? "?"} · nothing was run
      </div>
      <Warnings items={s.warnings} />
    </div>
  );
}

export function TestRunView({ data: s, compact }: ViewProps) {
  const counts = [["passed", s.passed, "bg-emerald-500"], ["failed", s.failed, "bg-rose-500"], ["errors", s.errors, "bg-orange-500"],
    ["skipped", s.skipped, "bg-slate-400"]] as const;
  const total = counts.reduce((sum, [, n]) => sum + (n ?? 0), 0) || 1;
  const tone = s.status === "passed" ? "good" : s.status === "no_tests" ? "warn" : "bad";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone={tone}>{String(s.status).replace("_", " ")}</Chip>
        <Chip mono>{s.framework}</Chip>
        <span className="text-[12px] text-[var(--text-faint)]">exit {s.exit_code ?? "—"} · {Number(s.duration_seconds ?? 0).toFixed(1)} s</span>
      </div>
      {s.total != null && (
        <>
          <div className="flex h-2.5 overflow-hidden rounded-full bg-[var(--panel-muted)]">
            {counts.map(([name, n, color]) => n ? <div key={name} className={color} style={{ width: `${(n / total) * 100}%` }} /> : null)}
          </div>
          <div className="grid grid-cols-4 gap-2">
            {counts.map(([name, n]) => <Stat key={name} label={name} value={n ?? "—"} tone={name === "passed" ? "good" : name === "skipped" ? undefined : n ? "bad" : undefined} />)}
          </div>
        </>
      )}
      {s.side_effects?.total_changes > 0 && (
        <div className="text-[12px] text-amber-600 dark:text-amber-400">The tests changed {s.side_effects.total_changes} file(s) in the workspace.</div>
      )}
      <Warnings items={s.warnings} />
      {!compact && <TestOutput stdout={s.stdout} stderr={s.stderr} />}
    </div>
  );
}

function TestOutput({ stdout, stderr }: { stdout?: string; stderr?: string }) {
  const [tab, setTab] = useState<"stderr" | "stdout">(stderr ? "stderr" : "stdout");
  const text = tab === "stderr" ? stderr : stdout;
  if (!stdout && !stderr) return null;
  return (
    <Section title="Output">
      <div className="flex gap-1">
        {(["stderr", "stdout"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`rounded-md px-2 py-0.5 font-mono text-[11px] ${tab === t ? "bg-indigo-500/15 text-[var(--text)]" : "text-[var(--text-faint)]"}`}>{t}</button>
        ))}
      </div>
      <CodeBlock code={text || "(empty)"} language="plaintext" maxHeight="50vh" />
    </Section>
  );
}

const CHECK_ICON = {
  passed: <CircleCheck size={15} className="text-emerald-500" />,
  failed: <CircleX size={15} className="text-rose-500" />,
  error: <CircleX size={15} className="text-rose-500" />,
  warning: <TriangleAlert size={15} className="text-amber-500" />,
  skipped: <CircleMinus size={15} className="text-[var(--text-faint)]" />,
} as Record<string, React.ReactNode>;

export function ValidateView({ data: s, compact }: ViewProps) {
  const checks = (s.checks ?? []) as Data[];
  const passed = checks.filter((c) => c.status === "passed").length;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-[12.5px]">
        <Chip tone={passed === checks.length ? "good" : s.failures?.length ? "bad" : "warn"}>{passed}/{checks.length} checks passed</Chip>
        {!s.tests?.execution_requested && <span className="text-[var(--text-faint)]">no code was executed</span>}
      </div>
      <div className="space-y-1.5">
        {checks.map((c) => (
          <div key={c.name} className="flex items-start gap-2 text-[12.5px]">
            <span className="mt-0.5">{CHECK_ICON[c.status] ?? CHECK_ICON.skipped}</span>
            <div className="min-w-0">
              <div className="font-medium">{c.name.replace(/_/g, " ")}</div>
              <div className="text-[11.5px] text-[var(--text-faint)]">{c.detail}</div>
            </div>
          </div>
        ))}
      </div>
      {!compact && (s.syntax?.errors ?? []).length > 0 && (
        <Section title="Syntax errors">
          {(s.syntax.errors as Data[]).map((e) => (
            <div key={`${e.path}:${e.line}`} className="flex gap-2 text-[12px]"><FileLink path={e.path} line={e.line} /><span className="truncate text-rose-500">{e.message}</span></div>
          ))}
        </Section>
      )}
      {!compact && <Warnings items={s.warnings} />}
    </div>
  );
}

function Warnings({ items }: { items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div className="space-y-1">
      {items.map((w) => (
        <div key={w} className="flex gap-2 text-[11.5px] text-amber-600 dark:text-amber-400"><TriangleAlert size={13} className="mt-0.5 shrink-0" />{w}</div>
      ))}
    </div>
  );
}
