import hljs from "highlight.js/lib/common";
import { useMemo } from "react";

const EXT_LANG: Record<string, string> = {
  py: "python", pyi: "python", ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript", mjs: "javascript",
  json: "json", md: "markdown", toml: "ini", cfg: "ini", ini: "ini", yml: "yaml", yaml: "yaml", sh: "bash",
  html: "xml", xml: "xml", svg: "xml", css: "css", go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", sql: "sql",
};

export function languageFor(path: string): string | undefined {
  return EXT_LANG[path.split(".").pop()?.toLowerCase() ?? ""];
}

const escapeHtml = (text: string) => text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);

/** highlight.js escapes its input, so the returned HTML never contains markup from the file itself. */
function highlight(code: string, language: string | undefined): string {
  try {
    if (language && hljs.getLanguage(language)) return hljs.highlight(code, { language, ignoreIllegals: true }).value;
    return code.length < 20_000 ? hljs.highlightAuto(code).value : escapeHtml(code);
  } catch {
    return escapeHtml(code);
  }
}

export function CodeBlock({ code, language, numbered, maxHeight, firstLine = 1 }: {
  code: string; language?: string; numbered?: boolean; maxHeight?: string; firstLine?: number;
}) {
  const shown = code.length > 150_000 ? code.slice(0, 150_000) : code;
  const html = useMemo(() => highlight(shown, language), [shown, language]);
  const lines = numbered ? shown.split("\n").length : 0;
  return (
    <div className="flex overflow-auto rounded-xl border border-[var(--border)] bg-[#0b1020] scroll-slim" style={{ maxHeight }}>
      {numbered && (
        <pre className="sticky left-0 select-none border-r border-white/5 bg-[#0b1020] py-3 pl-3 pr-3 text-right font-mono text-[11.5px] leading-[1.6] text-slate-600">
          {Array.from({ length: lines }, (_, i) => i + firstLine).join("\n")}
        </pre>
      )}
      <pre className="flex-1 py-3 pl-4 pr-4 font-mono text-[11.5px] leading-[1.6] text-slate-200">
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

export function DiffView({ patch, maxHeight = "42vh" }: { patch: string; maxHeight?: string }) {
  return (
    <pre className="overflow-auto rounded-xl border border-[var(--border)] bg-[#0b1020] py-2 font-mono text-[12px] leading-[1.55] scroll-slim" style={{ maxHeight }}>
      {patch.split("\n").map((line, i) => {
        const tone = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git") ? "font-semibold text-slate-300"
          : line.startsWith("+") ? "bg-emerald-500/12 text-emerald-300"
          : line.startsWith("-") ? "bg-rose-500/12 text-rose-300"
          : line.startsWith("@@") ? "text-cyan-300"
          : "text-slate-400";
        return <div key={i} className={`whitespace-pre px-4 ${tone}`}>{line || " "}</div>;
      })}
    </pre>
  );
}

/** Wraps case-insensitive occurrences of `term` in a highlight mark. */
export function Highlighted({ text, term }: { text: string; term?: string }) {
  if (!term) return <>{text}</>;
  const parts = text.split(new RegExp(`(${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"));
  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === term.toLowerCase()
          ? <mark key={i} className="rounded bg-amber-400/25 px-0.5 text-[var(--text)]">{part}</mark>
          : <span key={i}>{part}</span>,
      )}
    </>
  );
}
