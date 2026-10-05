import { Check, Copy, FileCode2, MessageSquarePlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { QueryResult } from "../types";
import { CodeBlock, languageFor } from "./CodeViews";
import { formatBytes } from "./results/parts";
import { ResultView } from "./results/ResultView";
import { SlideOver } from "./SlideOver";

const LINE_HEIGHT_PX = 18.4; // 11.5px font × 1.6 line height in CodeBlock

/** Opens a workspace file with a silent read_file query, scrolled to `line` when given. */
export function FileViewer({ target, onClose, query, onAsk, canAsk }: {
  target: { path: string; line?: number } | null;
  onClose: () => void;
  query: (name: string, args: Record<string, unknown>) => Promise<QueryResult>;
  onAsk: (path: string) => void;
  canAsk: boolean;
}) {
  const [result, setResult] = useState<QueryResult | null>(null);
  const [copied, setCopied] = useState(false);
  const body = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!target) return;
    let cancelled = false;
    setResult(null);
    query("read_file", { path: target.path }).then((r) => !cancelled && setResult(r));
    return () => { cancelled = true; };
  }, [target, query]);

  useEffect(() => {
    if (!result || result.status !== "ok" || !target?.line) return;
    const scroller = body.current?.closest(".overflow-y-auto");
    scroller?.scrollTo({ top: Math.max(0, (target.line - 6) * LINE_HEIGHT_PX), behavior: "smooth" });
  }, [result, target]);

  const s = result?.structured;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(s?.content ?? ""));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <SlideOver
      open={target !== null}
      onClose={onClose}
      width="max-w-3xl"
      icon={<FileCode2 size={18} className="text-indigo-400" />}
      title={
        <div className="min-w-0">
          <div className="truncate font-mono text-[13.5px] font-semibold">{target?.path}</div>
          {s && <div className="text-[11.5px] text-[var(--text-faint)]">{s.line_count} lines · {formatBytes(s.size_bytes)}{target?.line ? ` · line ${target.line}` : ""}</div>}
        </div>
      }
      actions={
        <>
          {result?.status === "ok" && (
            <button onClick={copy} className="rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--panel-muted)]" title="Copy file">
              {copied ? <Check size={16} className="text-emerald-400" /> : <Copy size={16} />}
            </button>
          )}
          {target && canAsk && (
            <button
              onClick={() => onAsk(target.path)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-indigo-500 to-cyan-500 px-3 py-1.5 text-[12.5px] font-semibold text-white shadow-md shadow-indigo-500/25"
            >
              <MessageSquarePlus size={14} /> Ask about this file
            </button>
          )}
        </>
      }
    >
      <div ref={body} className="p-5">
        {!result ? (
          <div className="shimmer h-1 rounded-full" />
        ) : result.status === "ok" && target ? (
          <CodeBlock code={String(s?.content ?? "")} language={languageFor(target.path)} numbered />
        ) : (
          <ResultView
            call={{ id: "viewer", name: "read_file", arguments: { path: target?.path }, access: "read", source: "user",
              status: result.status === "refused" ? "error" : result.status, startedAt: 0, text: result.text, structured: null }}
          />
        )}
      </div>
    </SlideOver>
  );
}
