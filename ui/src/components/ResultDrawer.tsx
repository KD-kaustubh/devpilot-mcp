import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { ACCESS, STATUS_LABEL, TOOL_LABEL, formatDuration } from "../lib";
import type { ToolCall } from "../types";
import { StatusIcon } from "./Activity";
import { CodeBlock } from "./CodeViews";
import { ResultView } from "./results/ResultView";
import { SlideOver } from "./SlideOver";

export function ResultDrawer({ call, onClose }: { call: ToolCall | null; onClose: () => void }) {
  const [tab, setTab] = useState<"overview" | "raw">("overview");
  const [copied, setCopied] = useState(false);
  useEffect(() => setTab("overview"), [call?.id]);

  const raw = call ? (call.structured ? JSON.stringify(call.structured, null, 2) : call.text ?? "") : "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch { /* clipboard unavailable */ }
  };

  return (
    <SlideOver
      open={call !== null}
      onClose={onClose}
      icon={call && <StatusIcon status={call.status} size={20} />}
      title={call && (
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold">{TOOL_LABEL[call.name] ?? call.name}</span>
            <span className="font-mono text-[11.5px] text-[var(--text-faint)]">{call.name}</span>
            <span className={`rounded-full border px-1.5 py-px text-[10.5px] ${ACCESS[call.access].badge}`}>{ACCESS[call.access].label}</span>
          </div>
          <div className="text-[12px] text-[var(--text-faint)]">
            {STATUS_LABEL[call.status]}{call.durationMs !== undefined && ` · ${formatDuration(call.durationMs)}`} · {call.source === "agent" ? "requested by the AI" : "requested by you"}
          </div>
        </div>
      )}
      actions={
        <button onClick={copy} className="rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--panel-muted)]" title="Copy raw result">
          {copied ? <Check size={16} className="text-emerald-400" /> : <Copy size={16} />}
        </button>
      }
    >
      {call && (
        <div className="space-y-4 p-5">
          <div className="inline-flex rounded-lg border border-[var(--border)] p-0.5 text-[12.5px]">
            {(["overview", "raw"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                className={`rounded-md px-3 py-1 transition ${tab === t ? "bg-indigo-500/15 font-medium text-[var(--text)]" : "text-[var(--text-muted)]"}`}>
                {t === "overview" ? "Overview" : "Raw JSON"}
              </button>
            ))}
          </div>
          {tab === "overview" ? (
            <ResultView call={call} />
          ) : (
            <div className="space-y-4">
              {call.arguments && Object.keys(call.arguments).length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">Arguments</div>
                  <CodeBlock code={JSON.stringify(call.arguments, null, 2)} language="json" />
                </div>
              )}
              <div className="space-y-1.5">
                <div className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">Result</div>
                <CodeBlock code={raw || "(no result yet)"} language={call.structured ? "json" : "plaintext"} />
              </div>
            </div>
          )}
        </div>
      )}
    </SlideOver>
  );
}
