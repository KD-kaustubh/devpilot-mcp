import { AnimatePresence, motion } from "framer-motion";
import { FilePenLine, ShieldCheck, Terminal } from "lucide-react";
import { useEffect } from "react";
import { ACCESS, TOOL_LABEL } from "../lib";
import type { ApprovalRequest } from "../types";

export function DiffView({ patch }: { patch: string }) {
  return (
    <pre className="max-h-[42vh] overflow-auto rounded-xl border border-[var(--border)] bg-[#0b1020] py-2 font-mono text-[12px] leading-[1.55] scroll-slim">
      {patch.split("\n").map((line, i) => {
        const tone = line.startsWith("+++") || line.startsWith("---") ? "text-slate-300 font-semibold"
          : line.startsWith("+") ? "bg-emerald-500/12 text-emerald-300"
          : line.startsWith("-") ? "bg-rose-500/12 text-rose-300"
          : line.startsWith("@@") ? "text-cyan-300"
          : "text-slate-400";
        return <div key={i} className={`whitespace-pre px-4 ${tone}`}>{line || " "}</div>;
      })}
    </pre>
  );
}

export function ApprovalDialog({ request, workspace, onAnswer }: {
  request: ApprovalRequest | null; workspace?: string; onAnswer: (callId: string, approved: boolean) => void;
}) {
  useEffect(() => {
    if (!request) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onAnswer(request.callId, false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, onAnswer]);

  const patch = request?.name === "apply_patch" && typeof request.arguments?.patch === "string" ? request.arguments.patch : null;
  const Icon = request?.access === "execute" ? Terminal : FilePenLine;

  return (
    <AnimatePresence>
      {request && (
        <motion.div
          key={request.callId}
          className="fixed inset-0 z-50 grid place-items-center bg-slate-950/55 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            initial={{ opacity: 0, y: 24, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.97 }}
            transition={{ type: "spring", stiffness: 300, damping: 26 }}
            className="w-full max-w-2xl rounded-2xl border border-[var(--border)] bg-[var(--panel-solid)] p-6 shadow-2xl shadow-black/40"
          >
            <div className="flex items-start gap-4">
              <motion.div
                animate={{ rotate: [0, -8, 8, 0] }}
                transition={{ duration: 0.6, delay: 0.15 }}
                className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl ${
                  request.access === "execute" ? "bg-rose-500/15 text-rose-400" : "bg-amber-500/15 text-amber-400"}`}
              >
                <Icon size={22} />
              </motion.div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <h2 className="text-lg font-semibold">Approve {TOOL_LABEL[request.name] ?? request.name}?</h2>
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] ${ACCESS[request.access].badge}`}>{ACCESS[request.access].label}</span>
                </div>
                <p className="mt-1 text-[13.5px] text-[var(--text-muted)]">
                  {request.access === "execute"
                    ? <>This runs the repository&apos;s test code on your computer, in <b>{workspace}</b>. Test code can read and write files and use the network.</>
                    : <>This modifies files in <b>{workspace}</b>. DevPilot applies exactly this change, and you can undo it with <span className="font-mono">revert_patch</span>.</>}
                </p>
              </div>
            </div>

            <div className="mt-5">
              {patch ? (
                <DiffView patch={patch} />
              ) : (
                <pre className="max-h-[36vh] overflow-auto rounded-xl border border-[var(--border)] bg-[var(--panel-muted)] p-4 font-mono text-[12px] scroll-slim">
                  <span className="text-[var(--text-faint)]">{request.name}</span>
                  {"\n"}{JSON.stringify(request.arguments ?? {}, null, 2)}
                </pre>
              )}
            </div>

            <div className="mt-6 flex items-center gap-3">
              <span className="inline-flex items-center gap-1.5 text-[12px] text-[var(--text-faint)]">
                <ShieldCheck size={14} /> Nothing runs until you approve
              </span>
              <button
                onClick={() => onAnswer(request.callId, false)}
                className="ml-auto rounded-xl border border-[var(--border)] px-4 py-2 text-[13.5px] font-medium transition hover:bg-[var(--panel-muted)]"
              >
                Deny <span className="ml-1 text-[11px] text-[var(--text-faint)]">Esc</span>
              </button>
              <motion.button
                autoFocus
                whileTap={{ scale: 0.96 }}
                onClick={() => onAnswer(request.callId, true)}
                className={`rounded-xl px-4 py-2 text-[13.5px] font-semibold text-white shadow-lg transition ${
                  request.access === "execute" ? "bg-gradient-to-br from-rose-500 to-orange-500 shadow-rose-500/25"
                    : "bg-gradient-to-br from-indigo-500 to-cyan-500 shadow-indigo-500/25"}`}
              >
                Approve
              </motion.button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
