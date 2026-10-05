import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, CornerDownLeft, Play, Search, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ACCESS, TOOL_LABEL } from "../lib";
import type { ToolInfo } from "../types";
import { SchemaForm, fieldsOf, initialValues, toArguments } from "./SchemaForm";

export function CommandPalette({ open, tools, initialTool, disabled, onClose, onRun }: {
  open: boolean;
  tools: ToolInfo[];
  initialTool: string | null;
  disabled: boolean;
  onClose: () => void;
  onRun: (name: string, args: Record<string, unknown>, label: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<ToolInfo | null>(null);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [missing, setMissing] = useState<string[]>([]);
  const searchBox = useRef<HTMLInputElement>(null);

  const fields = useMemo(() => (selected ? fieldsOf(selected.input_schema ?? {}) : []), [selected]);

  const choose = (tool: ToolInfo) => {
    setSelected(tool);
    setValues(initialValues(fieldsOf(tool.input_schema ?? {})));
    setMissing([]);
  };

  useEffect(() => {
    if (!open) return;
    setSearch("");
    setCursor(0);
    const preset = tools.find((t) => t.name === initialTool);
    if (preset) choose(preset);
    else {
      setSelected(null);
      requestAnimationFrame(() => searchBox.current?.focus());
    }
  }, [open, initialTool]);

  const matches = useMemo(() => {
    const q = search.trim().toLowerCase();
    return tools.filter((t) => !q || [t.name, TOOL_LABEL[t.name] ?? "", t.summary].some((s) => s.toLowerCase().includes(q)));
  }, [tools, search]);

  const run = () => {
    if (!selected || disabled) return;
    const { args, missing: absent } = toArguments(fields, values);
    if (absent.length) {
      setMissing(absent);
      return;
    }
    onRun(selected.name, args, TOOL_LABEL[selected.name] ?? selected.name);
    onClose();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      if (selected && !initialTool) setSelected(null);
      else onClose();
    } else if (!selected && e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, matches.length - 1));
    } else if (!selected && e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (!selected && e.key === "Enter" && matches[cursor]) {
      e.preventDefault();
      choose(matches[cursor]);
    } else if (selected && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      run();
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-start justify-center bg-slate-950/50 p-4 pt-[12vh] backdrop-blur-sm"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          onMouseDown={(e) => e.target === e.currentTarget && onClose()}
          onKeyDown={onKeyDown}
        >
          <motion.div
            initial={{ opacity: 0, y: -12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 320, damping: 28 }}
            className="flex max-h-[72vh] w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--panel-solid)] shadow-2xl shadow-black/40"
          >
            {!selected ? (
              <>
                <div className="flex items-center gap-2.5 border-b border-[var(--border)] px-4 py-3">
                  <Search size={16} className="text-[var(--text-faint)]" />
                  <input
                    ref={searchBox}
                    value={search}
                    onChange={(e) => { setSearch(e.target.value); setCursor(0); }}
                    placeholder="Run a tool… (search code, git diff, issues, read file)"
                    className="flex-1 bg-transparent text-[14px] outline-none placeholder:text-[var(--text-faint)]"
                  />
                  <kbd className="rounded border border-[var(--border)] px-1.5 text-[10.5px] text-[var(--text-faint)]">Esc</kbd>
                </div>
                <div className="min-h-0 overflow-y-auto p-1.5 scroll-slim">
                  {matches.map((tool, i) => (
                    <button
                      key={tool.name}
                      onMouseEnter={() => setCursor(i)}
                      onClick={() => choose(tool)}
                      className={`flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition ${i === cursor ? "bg-indigo-500/12" : ""}`}
                    >
                      <span className={`h-2 w-2 shrink-0 rounded-full ${ACCESS[tool.access].dot}`} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="text-[13.5px] font-medium">{TOOL_LABEL[tool.name] ?? tool.name}</span>
                          <span className="font-mono text-[11px] text-[var(--text-faint)]">{tool.name}</span>
                        </span>
                        <span className="block truncate text-[12px] text-[var(--text-muted)]">{tool.summary}</span>
                      </span>
                      {i === cursor && <CornerDownLeft size={14} className="text-[var(--text-faint)]" />}
                    </button>
                  ))}
                  {matches.length === 0 && <div className="px-3 py-6 text-center text-[13px] text-[var(--text-faint)]">No tool matches “{search}”.</div>}
                </div>
              </>
            ) : (
              <>
                <div className="flex items-center gap-2.5 border-b border-[var(--border)] px-4 py-3">
                  {!initialTool && (
                    <button onClick={() => setSelected(null)} className="rounded-md p-1 text-[var(--text-faint)] hover:bg-[var(--panel-muted)]" title="Back">
                      <ArrowLeft size={15} />
                    </button>
                  )}
                  <span className="font-semibold">{TOOL_LABEL[selected.name] ?? selected.name}</span>
                  <span className={`rounded-full border px-2 py-0.5 text-[10.5px] ${ACCESS[selected.access].badge}`}>{ACCESS[selected.access].label}</span>
                </div>
                <div className="min-h-0 space-y-4 overflow-y-auto px-4 py-4 scroll-slim">
                  <p className="text-[12.5px] text-[var(--text-muted)]">{selected.summary}</p>
                  <SchemaForm fields={fields} values={values} onChange={(n, v) => setValues((old) => ({ ...old, [n]: v }))} onSubmit={run} />
                  {missing.length > 0 && <div className="text-[12px] text-rose-500">Please fill in: {missing.join(", ")}</div>}
                </div>
                <div className="flex items-center gap-3 border-t border-[var(--border)] px-4 py-3">
                  {selected.access !== "read" && (
                    <span className="inline-flex items-center gap-1.5 text-[11.5px] text-[var(--text-faint)]"><ShieldCheck size={13} /> asks for your approval first</span>
                  )}
                  <button
                    onClick={run}
                    disabled={disabled}
                    title={disabled ? "Wait for the current request to finish" : "Run (Ctrl+Enter)"}
                    className="ml-auto inline-flex items-center gap-1.5 rounded-xl bg-gradient-to-br from-indigo-500 to-cyan-500 px-4 py-2 text-[13px] font-semibold text-white shadow-lg shadow-indigo-500/25 disabled:opacity-40"
                  >
                    <Play size={14} /> Run
                  </button>
                </div>
              </>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
