import { AnimatePresence, motion } from "framer-motion";
import hljs from "highlight.js/lib/common";
import { Check, Copy, X } from "lucide-react";
import { useMemo, useState } from "react";
import { ACCESS, STATUS_LABEL, TOOL_LABEL, formatDuration } from "../lib";
import type { ToolCall } from "../types";
import { StatusIcon } from "./Activity";

const EXT_LANG: Record<string, string> = {
  py: "python", pyi: "python", ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
  json: "json", md: "markdown", toml: "ini", cfg: "ini", ini: "ini", yml: "yaml", yaml: "yaml", sh: "bash",
  html: "xml", xml: "xml", css: "css", go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", sql: "sql",
};

function highlight(code: string, language: string | undefined): string {
  try {
    return language && hljs.getLanguage(language)
      ? hljs.highlight(code, { language, ignoreIllegals: true }).value
      : hljs.highlightAuto(code).value;
  } catch {
    return code.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
  }
}

function CodeBlock({ code, language, numbered }: { code: string; language?: string; numbered?: boolean }) {
  const html = useMemo(() => highlight(code.length > 150_000 ? code.slice(0, 150_000) : code, language), [code, language]);
  const lines = numbered ? code.split("\n").length : 0;
  return (
    <div className="flex overflow-auto rounded-xl border border-[var(--border)] bg-[#0b1020] scroll-slim">
      {numbered && (
        <pre className="select-none border-r border-white/5 py-3 pl-3 pr-3 text-right font-mono text-[11.5px] leading-[1.6] text-slate-600">
          {Array.from({ length: lines }, (_, i) => i + 1).join("\n")}
        </pre>
      )}
      <pre className="flex-1 py-3 pl-4 pr-4 font-mono text-[11.5px] leading-[1.6] text-slate-200">
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

export function ResultDrawer({ call, onClose }: { call: ToolCall | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const file = call?.name === "read_file" && call.status === "ok" && typeof call.structured?.content === "string"
    ? { path: String(call.structured.path), content: call.structured.content as string } : null;
  const body = file ? file.content
    : call?.structured ? JSON.stringify(call.structured, null, 2) : call?.text ?? "";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(body);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard can be unavailable */
    }
  };

  return (
    <AnimatePresence>
      {call && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-slate-950/40 backdrop-blur-[2px]"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className="fixed inset-y-0 right-0 z-40 flex w-full max-w-2xl flex-col border-l border-[var(--border)] bg-[var(--panel-solid)] shadow-2xl"
            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 280, damping: 32 }}
          >
            <div className="flex items-center gap-3 border-b border-[var(--border)] px-5 py-4">
              <StatusIcon status={call.status} size={20} />
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono font-semibold">{call.name}</span>
                  <span className={`rounded-full border px-1.5 py-px text-[10.5px] ${ACCESS[call.access].badge}`}>{ACCESS[call.access].label}</span>
                </div>
                <div className="text-[12px] text-[var(--text-faint)]">
                  {TOOL_LABEL[call.name] ?? call.name} · {STATUS_LABEL[call.status]}
                  {call.durationMs !== undefined && ` · ${formatDuration(call.durationMs)}`} · {call.source === "agent" ? "requested by the AI" : "requested by you"}
                </div>
              </div>
              <button onClick={copy} className="ml-auto rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--panel-muted)]" title="Copy result">
                {copied ? <Check size={16} className="text-emerald-400" /> : <Copy size={16} />}
              </button>
              <button onClick={onClose} className="rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--panel-muted)]" title="Close">
                <X size={16} />
              </button>
            </div>

            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-5 scroll-slim">
              {call.arguments && Object.keys(call.arguments).length > 0 && (
                <section>
                  <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">Arguments</h3>
                  <CodeBlock code={JSON.stringify(call.arguments, null, 2)} language="json" />
                </section>
              )}
              <section>
                <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">
                  {file ? file.path : "Result"}
                </h3>
                {call.status === "running" || call.status === "awaiting" ? (
                  <div className="shimmer h-1 rounded-full" />
                ) : call.status !== "ok" ? (
                  <div className={`rounded-xl border p-4 text-[13px] ${
                    call.status === "blocked" ? "border-rose-500/35 bg-rose-500/10 text-rose-600 dark:text-rose-300" : "border-[var(--border)] bg-[var(--panel-muted)]"}`}>
                    {call.text}
                  </div>
                ) : file ? (
                  <CodeBlock code={file.content} language={EXT_LANG[file.path.split(".").pop()?.toLowerCase() ?? ""]} numbered />
                ) : (
                  <CodeBlock code={body} language={call.structured ? "json" : undefined} />
                )}
              </section>
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
