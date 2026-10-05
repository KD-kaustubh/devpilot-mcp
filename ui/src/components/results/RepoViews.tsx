import { File, Folder } from "lucide-react";
import { CodeBlock, Highlighted, languageFor } from "../CodeViews";
import { Chip, Empty, FileLink, LanguageBar, More, Section, Stat, formatBytes, take, useResultActions } from "./parts";

type Data = Record<string, any>;
interface ViewProps { data: Data; compact: boolean; callId: string }

export function AnalyzeView({ data: s, compact, callId }: ViewProps) {
  const [dirs, moreDirs] = take(s.directories as Data[], compact, 5);
  const frameworks = (s.heuristics?.framework_indicators ?? []) as Data[];
  const entries = (s.heuristics?.possible_entry_points ?? []) as Data[];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="files" value={s.total_files} />
        <Stat label="languages" value={Object.keys(s.languages ?? {}).length} />
        <Stat label="test files" value={s.tests?.file_count ?? 0} />
        <Stat label="manifests" value={(s.dependency_manifests ?? []).length} />
      </div>
      {Object.keys(s.languages ?? {}).length > 0 && <Section title="Languages"><LanguageBar languages={s.languages} /></Section>}
      {dirs.length > 0 && (
        <Section title="Top directories">
          <div className="space-y-1">
            {dirs.map((d) => (
              <div key={d.path} className="flex items-center gap-2 text-[12.5px]">
                <Folder size={13} className="text-indigo-400" />
                <span className="font-mono">{d.path}</span>
                <span className="ml-auto tabular-nums text-[var(--text-faint)]">{d.file_count} files</span>
              </div>
            ))}
            <More hidden={moreDirs} callId={callId} />
          </div>
        </Section>
      )}
      {(frameworks.length > 0 || entries.length > 0 || (s.dependency_manifests ?? []).length > 0) && (
        <Section title="Stack and entry points">
          <div className="flex flex-wrap gap-1.5">
            {frameworks.map((f) => <Chip key={f.name + f.file} tone="info" title={`${f.file}: ${f.evidence}`}>{f.name}</Chip>)}
            {(s.dependency_manifests ?? []).slice(0, compact ? 4 : 50).map((m: string) => <Chip key={m} mono>{m}</Chip>)}
            {compact && entries.slice(0, 3).map((e) => <Chip key={e.file + e.detail} mono tone="good" title={e.detail}>▶ {e.file}</Chip>)}
          </div>
          {!compact && entries.length > 0 && (
            <div className="space-y-1 pt-1">
              {entries.map((e) => (
                <div key={e.file + e.detail} className="flex items-center gap-2 text-[12px]">
                  <FileLink path={e.file} /> <span className="truncate text-[var(--text-faint)]">{e.detail}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      )}
    </div>
  );
}

export function DirectoryView({ data: s, compact, callId }: ViewProps) {
  const [entries, more] = take(s.entries as Data[], compact, 10);
  return (
    <div className="space-y-1.5">
      <div className="text-[12px] text-[var(--text-faint)]"><span className="font-mono">{s.path}</span> · {s.total_entries} entries</div>
      <div className="divide-y divide-[var(--border)] rounded-xl border border-[var(--border)]">
        {entries.map((e) => (
          <div key={e.path} className="flex items-center gap-2 px-3 py-1.5 text-[12.5px]">
            {e.type === "directory" ? <Folder size={14} className="text-indigo-400" /> : <File size={14} className="text-[var(--text-faint)]" />}
            {e.type === "file" ? <FileLink path={e.path} label={e.name} /> : <span className="font-mono">{e.name}</span>}
            <span className="ml-auto tabular-nums text-[11px] text-[var(--text-faint)]">{formatBytes(e.size_bytes)}</span>
          </div>
        ))}
        {entries.length === 0 && <div className="px-3 py-2 text-[12px] text-[var(--text-faint)]">Empty directory</div>}
      </div>
      <More hidden={more} callId={callId} />
    </div>
  );
}

export function FileView({ data: s, compact }: ViewProps) {
  const { openFile } = useResultActions();
  const lines = String(s.content ?? "").split("\n");
  const shown = compact ? lines.slice(0, 14).join("\n") : s.content;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-[12px] text-[var(--text-faint)]">
        <span className="font-mono text-[var(--text)]">{s.path}</span> · {s.line_count} lines · {formatBytes(s.size_bytes)}
        {compact && <button onClick={() => openFile(s.path)} className="ml-auto text-indigo-600 hover:underline dark:text-indigo-300">Open</button>}
      </div>
      <CodeBlock code={shown} language={languageFor(s.path)} numbered maxHeight={compact ? "260px" : undefined} />
    </div>
  );
}

/** search_code ({file, line, text}) and search_files ({path, line_number, line}), grouped by file. */
export function SearchView({ data: s, compact, callId }: ViewProps) {
  // search_files: {path, line_number, line (text)}; search_code: {file, line (number), text}.
  const normalized: { file: string; line: number; text: string }[] = (s.matches ?? []).map((m: Data) =>
    "line_number" in m ? { file: m.path, line: m.line_number, text: m.line } : { file: m.file, line: m.line, text: m.text });
  const groups = new Map<string, { line: number; text: string }[]>();
  for (const m of normalized) groups.set(m.file, [...(groups.get(m.file) ?? []), { line: m.line, text: m.text }]);
  const [files, more] = take([...groups.entries()], compact, 4);
  if (normalized.length === 0) return <Empty>No matches for “{s.query}” in {s.files_searched} files.</Empty>;
  return (
    <div className="space-y-2.5">
      <div className="text-[12px] text-[var(--text-faint)]">
        {normalized.length}{s.truncated ? "+" : ""} matches in {groups.size} files · {s.files_searched} files searched
        {s.case_sensitive !== undefined && ` · ${s.case_sensitive ? "case-sensitive" : "any case"}`}
      </div>
      {files.map(([file, rows]) => (
        <div key={file} className="overflow-hidden rounded-xl border border-[var(--border)]">
          <div className="border-b border-[var(--border)] bg-[var(--panel-muted)]/60 px-3 py-1.5"><FileLink path={file} /></div>
          {rows.slice(0, compact ? 3 : 100).map((r) => (
            <FileLine key={r.line} file={file} line={r.line} text={r.text} term={s.query} />
          ))}
        </div>
      ))}
      <More hidden={more} callId={callId} />
    </div>
  );
}

function FileLine({ file, line, text, term }: { file: string; line: number; text: string; term?: string }) {
  const { openFile } = useResultActions();
  return (
    <button onClick={() => openFile(file, line)} className="flex w-full gap-3 px-3 py-1 text-left font-mono text-[11.5px] hover:bg-indigo-500/5">
      <span className="w-9 shrink-0 text-right tabular-nums text-[var(--text-faint)]">{line}</span>
      <span className="truncate text-[var(--text-muted)]"><Highlighted text={text} term={term} /></span>
    </button>
  );
}

export function InvestigateView({ data: s, compact, callId }: ViewProps) {
  const [files, more] = take(s.relevant_files as Data[], compact, 5);
  const top = Math.max(1, ...((s.relevant_files ?? []) as Data[]).map((f) => f.score ?? 0));
  const commits = (s.git_context?.relevant_commits ?? []) as Data[];
  const issues = [...(s.github_context?.relevant_issues ?? []), ...(s.github_context?.relevant_pull_requests ?? [])] as Data[];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1.5">
        {(s.search_terms ?? []).map((t: Data) => <Chip key={t.term} tone={t.source === "query" ? "info" : undefined} mono>{t.term}</Chip>)}
      </div>
      <Section title="Most relevant files" aside={`${s.files_scanned} files scanned`}>
        {files.length === 0 ? <Empty>Nothing in the repository matched these terms.</Empty> : (
          <div className="space-y-2">
            {files.map((f) => (
              <div key={f.path} className="space-y-1">
                <div className="flex items-center gap-2">
                  <FileLink path={f.path} />
                  <Chip>{f.kind}</Chip>
                  <span className="ml-auto tabular-nums text-[11px] text-[var(--text-faint)]">{f.score}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-[var(--panel-muted)]">
                  <div className="h-full rounded-full bg-gradient-to-r from-indigo-400 to-cyan-400" style={{ width: `${(f.score / top) * 100}%` }} />
                </div>
                {!compact && <div className="text-[11.5px] text-[var(--text-faint)]">{(f.reasons ?? []).join(" · ")}</div>}
              </div>
            ))}
            <More hidden={more} callId={callId} />
          </div>
        )}
      </Section>
      {!compact && (s.file_context ?? []).length > 0 && (
        <Section title="Excerpts">
          <div className="space-y-2">
            {(s.file_context as Data[]).map((x) => (
              <div key={`${x.path}:${x.start_line}`} className="space-y-1">
                <FileLink path={x.path} line={x.start_line} />
                <CodeBlock code={x.content} language={languageFor(x.path)} numbered firstLine={x.start_line} />
              </div>
            ))}
          </div>
        </Section>
      )}
      {!compact && commits.length > 0 && (
        <Section title="Related commits">
          {commits.map((c) => (
            <div key={c.hash ?? c.short_hash} className="flex gap-2 text-[12.5px]">
              <span className="font-mono text-cyan-500">{c.short_hash}</span><span className="truncate">{c.subject}</span>
            </div>
          ))}
        </Section>
      )}
      {!compact && issues.length > 0 && (
        <Section title="Related issues and pull requests">
          {issues.map((i) => (
            <a key={i.html_url ?? i.number} href={i.html_url} target="_blank" rel="noreferrer" className="flex gap-2 text-[12.5px] hover:underline">
              <span className="text-[var(--text-faint)]">#{i.number}</span><span className="truncate">{i.title}</span>
            </a>
          ))}
        </Section>
      )}
    </div>
  );
}
