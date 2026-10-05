import { AnimatePresence, motion } from "framer-motion";
import { ArrowUp, Bot, CircleX, KeyRound, PanelRightOpen, Sparkles, SquarePen, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { SUGGESTIONS, TOOL_LABEL, formatDuration, resultSummary } from "../lib";
import type { Studio } from "../useStudio";
import type { ChatMessage, ToolCall } from "../types";
import { StatusIcon } from "./Activity";
import { Markdown } from "./Markdown";

export function ChatPanel({ studio, onOpenCall, onOpenFile, onToggleActivity }: {
  studio: Studio;
  onOpenCall: (id: string) => void;
  onOpenFile: (path: string) => void;
  onToggleActivity?: () => void;
}) {
  const { messages, calls, busy, status, connection } = studio;
  const scroller = useRef<HTMLDivElement>(null);
  const aiOff = status !== null && !status.ai.configured;

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-3 border-b border-[var(--border)] px-6 py-3.5">
        <Sparkles size={16} className="text-indigo-400" />
        <h1 className="font-semibold">Chat</h1>
        {status && <span className="truncate text-[12.5px] text-[var(--text-faint)]">about {status.workspace.name}</span>}
        <div className="ml-auto flex items-center gap-1">
          {connection !== "open" && (
            <span className="mr-2 inline-flex items-center gap-1.5 text-[12px] text-amber-400">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" /> reconnecting
            </span>
          )}
          <button
            onClick={studio.reset}
            disabled={busy || messages.length === 0}
            className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] text-[var(--text-muted)] transition hover:bg-[var(--panel-muted)] hover:text-[var(--text)] disabled:opacity-40"
          >
            <SquarePen size={14} /> New chat
          </button>
          {onToggleActivity && (
            <button onClick={onToggleActivity} className="rounded-lg p-1.5 text-[var(--text-muted)] hover:bg-[var(--panel-muted)] xl:hidden" title="Activity">
              <PanelRightOpen size={16} />
            </button>
          )}
        </div>
      </header>

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto scroll-slim">
        <div className="mx-auto w-full max-w-3xl px-6 py-6">
          {messages.length === 0 ? (
            <EmptyState name={status?.workspace.name} aiOff={aiOff} onPick={studio.ask} disabled={busy || connection !== "open" || aiOff} />
          ) : (
            <div className="space-y-5">
              <AnimatePresence initial={false}>
                {messages.map((m) => (
                  <MessageView key={m.id} message={m} calls={calls} onOpenCall={onOpenCall} onOpenFile={onOpenFile} />
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>
      </div>

      <Composer
        busy={busy}
        disabled={connection !== "open" || aiOff}
        placeholder={aiOff ? "AI chat is off: set AIPIPE_TOKEN and restart devpilot-ui. Quick actions still work." : `Ask anything about ${status?.workspace.name ?? "this repository"}…`}
        onSend={studio.ask}
      />
    </div>
  );
}

function EmptyState({ name, aiOff, onPick, disabled }: { name?: string; aiOff: boolean; onPick: (t: string) => void; disabled: boolean }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="pt-[8vh] text-center">
      <motion.div
        animate={{ rotate: [45, 50, 40, 45], y: [0, -4, 0] }}
        transition={{ repeat: Infinity, duration: 6, ease: "easeInOut" }}
        className="mx-auto mb-6 h-14 w-14 rounded-2xl bg-gradient-to-br from-indigo-400 to-cyan-400 shadow-2xl shadow-indigo-500/40"
      />
      <h2 className="text-3xl font-semibold tracking-tight">
        Ask about <span className="text-gradient">{name ?? "your repository"}</span>
      </h2>
      <p className="mx-auto mt-3 max-w-md text-[14px] text-[var(--text-muted)]">
        DevPilot reads, searches and inspects the code through its MCP tools. Watch every call on the right.
      </p>
      {aiOff ? (
        <div className="glass mx-auto mt-8 max-w-md rounded-2xl p-4 text-left text-[13px]">
          <div className="mb-1 flex items-center gap-2 font-semibold"><KeyRound size={15} className="text-amber-400" /> Turn on AI chat</div>
          <div className="text-[var(--text-muted)]">
            Get a token at <span className="font-mono">aipipe.org/login</span>, put <span className="font-mono">AIPIPE_TOKEN=…</span> in your
            <span className="font-mono"> .env</span>, then restart <span className="font-mono">devpilot-ui</span>. Until then, use the quick actions on the left.
          </div>
        </div>
      ) : (
        <div className="mx-auto mt-8 grid max-w-xl gap-2.5 sm:grid-cols-2">
          {SUGGESTIONS.map((s, i) => (
            <motion.button
              key={s}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.15 + i * 0.07 }}
              whileHover={{ y: -2 }}
              disabled={disabled}
              onClick={() => onPick(s)}
              className="glass rounded-xl px-4 py-3 text-left text-[13px] text-[var(--text-muted)] transition hover:border-indigo-400/40 hover:text-[var(--text)] disabled:opacity-50"
            >
              {s}
            </motion.button>
          ))}
        </div>
      )}
    </motion.div>
  );
}

function CallPill({ call, onOpen }: { call: ToolCall; onOpen: (id: string) => void }) {
  return (
    <motion.button
      initial={{ opacity: 0, scale: 0.85 }}
      animate={{ opacity: 1, scale: 1 }}
      onClick={() => onOpen(call.id)}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[11px] transition hover:border-indigo-400/50 ${
        call.status === "blocked" ? "border-rose-500/40 text-rose-600 dark:text-rose-300" : "border-[var(--border)] text-[var(--text-muted)]"}`}
    >
      <StatusIcon status={call.status} size={12} />
      {call.name}
      {call.durationMs !== undefined && <span className="text-[var(--text-faint)]">{formatDuration(call.durationMs)}</span>}
    </motion.button>
  );
}

function MessageView({ message, calls, onOpenCall, onOpenFile }: {
  message: ChatMessage; calls: Record<string, ToolCall>; onOpenCall: (id: string) => void; onOpenFile: (p: string) => void;
}) {
  const own = message.callIds.map((id) => calls[id]).filter(Boolean);
  const enter = { initial: { opacity: 0, y: 14 }, animate: { opacity: 1, y: 0 }, transition: { type: "spring" as const, stiffness: 260, damping: 26 } };

  if (message.role === "user") {
    return (
      <motion.div {...enter} className="flex justify-end">
        <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-gradient-to-br from-indigo-500 to-indigo-600 px-4 py-2.5 text-[14px] text-white shadow-lg shadow-indigo-500/20">
          {message.text}
        </div>
      </motion.div>
    );
  }

  if (message.role === "error") {
    return (
      <motion.div {...enter} className="flex items-start gap-2.5 rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-[13px] text-rose-600 dark:text-rose-300">
        <CircleX size={16} className="mt-0.5 shrink-0" /> {message.text}
      </motion.div>
    );
  }

  if (message.role === "action") {
    const call = own[0];
    return (
      <motion.div {...enter} className="glass rounded-2xl p-4">
        <div className="flex items-center gap-2 text-[13px]">
          <Zap size={15} className="text-cyan-400" />
          <span className="font-semibold">{message.text}</span>
          <span className="text-[var(--text-faint)]">quick action</span>
          {call && <span className="ml-auto"><StatusIcon status={call.status} size={16} /></span>}
        </div>
        {call && call.status !== "running" && call.status !== "awaiting" && (
          <div className="mt-2 flex items-center gap-3">
            <div className={`min-w-0 flex-1 truncate text-[13px] ${call.status === "ok" ? "text-[var(--text-muted)]" : "text-rose-600 dark:text-rose-300"}`}>
              {resultSummary(call)}
            </div>
            <button onClick={() => onOpenCall(call.id)} className="shrink-0 rounded-lg border border-[var(--border)] px-2.5 py-1 text-[12px] hover:border-indigo-400/50">
              View result
            </button>
          </div>
        )}
        {call?.status === "awaiting" && <div className="mt-2 text-[12.5px] text-amber-600 dark:text-amber-300">Waiting for your approval…</div>}
      </motion.div>
    );
  }

  return (
    <motion.div {...enter} className="flex gap-3">
      <div className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500/30 to-cyan-500/25 text-indigo-300">
        <Bot size={16} />
      </div>
      <div className="min-w-0 flex-1">
        {own.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {own.map((call) => <CallPill key={call.id} call={call} onOpen={onOpenCall} />)}
          </div>
        )}
        {message.text ? (
          <div className={message.streaming ? "caret" : ""}>
            <Markdown text={message.text} onOpenFile={onOpenFile} />
          </div>
        ) : message.streaming ? (
          <ThinkingDots label={own.length ? `Using ${TOOL_LABEL[own[own.length - 1].name] ?? "tools"}…` : "Thinking…"} />
        ) : null}
      </div>
    </motion.div>
  );
}

function ThinkingDots({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[13px] text-[var(--text-muted)]">
      <span className="flex gap-1">
        {[0, 1, 2].map((i) => (
          <motion.span
            key={i}
            className="h-1.5 w-1.5 rounded-full bg-gradient-to-r from-indigo-400 to-cyan-400"
            animate={{ y: [0, -5, 0], opacity: [0.5, 1, 0.5] }}
            transition={{ repeat: Infinity, duration: 0.9, delay: i * 0.15 }}
          />
        ))}
      </span>
      {label}
    </div>
  );
}

function Composer({ busy, disabled, placeholder, onSend }: { busy: boolean; disabled: boolean; placeholder: string; onSend: (t: string) => void }) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [text]);

  const submit = () => {
    const value = text.trim();
    if (!value || busy || disabled) return;
    onSend(value);
    setText("");
  };

  return (
    <div className="px-6 pb-5 pt-2">
      <div className="mx-auto max-w-3xl">
        <div className={`rounded-2xl p-[1.5px] transition ${busy ? "working-ring" : "bg-[var(--border)]"}`}>
          <div className="flex items-end gap-2 rounded-[15px] bg-[var(--panel-solid)] p-2">
            <textarea
              ref={area}
              rows={1}
              value={text}
              disabled={disabled}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={placeholder}
              className="max-h-[180px] min-h-[40px] flex-1 resize-none bg-transparent px-2.5 py-2 text-[14px] outline-none placeholder:text-[var(--text-faint)] disabled:cursor-not-allowed"
            />
            <motion.button
              whileTap={{ scale: 0.92 }}
              onClick={submit}
              disabled={busy || disabled || !text.trim()}
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-cyan-500 text-white shadow-lg shadow-indigo-500/30 transition disabled:from-slate-600 disabled:to-slate-600 disabled:opacity-40 disabled:shadow-none"
              title="Send (Enter)"
            >
              {busy ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" /> : <ArrowUp size={18} />}
            </motion.button>
          </div>
        </div>
        <div className="mt-2 text-center text-[11px] text-[var(--text-faint)]">
          Enter to send · Shift+Enter for a new line · changes and test runs always ask for your approval
        </div>
      </div>
    </div>
  );
}
