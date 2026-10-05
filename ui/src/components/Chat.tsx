import { AnimatePresence, motion } from "framer-motion";
import { Activity, ArrowUp, Bot, CircleX, Command, GitCompareArrows, KeyRound, Maximize2, Sparkles, SquarePen, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { SUGGESTIONS, TOOL_LABEL } from "../lib";
import type { Studio } from "../useStudio";
import type { ChatMessage, ToolCall } from "../types";
import { StatusIcon } from "./Activity";
import { Markdown } from "./Markdown";
import { ResultView } from "./results/ResultView";
import { Steps } from "./Steps";

export function ChatPanel({ studio, onOpenCall, onOpenFile, onOpenActivity, onOpenChanges, onOpenPalette }: {
  studio: Studio;
  onOpenCall: (id: string) => void;
  onOpenFile: (path: string) => void;
  onOpenActivity: () => void;
  onOpenChanges: () => void;
  onOpenPalette: () => void;
}) {
  const { messages, calls, order, busy, status, connection, changes } = studio;
  const scroller = useRef<HTMLDivElement>(null);
  const aiOff = status !== null && !status.ai.configured;
  const live = order.filter((id) => calls[id]?.status === "running" || calls[id]?.status === "awaiting").length;
  const applied = changes.filter((c) => !c.undone).length;

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, calls]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b border-[var(--border)] px-6 py-3">
        <Sparkles size={16} className="text-indigo-400" />
        <h1 className="font-semibold">Chat</h1>
        {status && <span className="hidden truncate text-[12.5px] text-[var(--text-faint)] sm:inline">about {status.workspace.name}</span>}
        <div className="ml-auto flex items-center gap-1">
          {connection !== "open" && (
            <span className="mr-2 inline-flex items-center gap-1.5 text-[12px] text-amber-500">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" /> reconnecting
            </span>
          )}
          <HeaderButton onClick={onOpenPalette} title="Run any tool (Ctrl+K)">
            <Command size={14} /> Tools <kbd className="hidden rounded border border-[var(--border)] px-1 text-[10px] text-[var(--text-faint)] md:inline">Ctrl K</kbd>
          </HeaderButton>
          <HeaderButton onClick={onOpenActivity} title="Every tool call in this session">
            <span className="relative">
              <Activity size={14} />
              {live > 0 && <span className="absolute -right-1 -top-1 h-2 w-2 animate-ping rounded-full bg-cyan-400" />}
            </span>
            Activity
            {order.length > 0 && <Badge tone={live ? "live" : undefined}>{live || order.length}</Badge>}
          </HeaderButton>
          <HeaderButton onClick={onOpenChanges} title="Changes applied in this session">
            <GitCompareArrows size={14} /> Changes {applied > 0 && <Badge tone="warn">{applied}</Badge>}
          </HeaderButton>
          <HeaderButton onClick={studio.reset} disabled={busy || messages.length === 0} title="Start a new conversation">
            <SquarePen size={14} /> New
          </HeaderButton>
        </div>
      </header>

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto scroll-slim">
        <div className="mx-auto w-full max-w-3xl px-6 py-6">
          {messages.length === 0 ? (
            <EmptyState name={status?.workspace.name} aiOff={aiOff} onPick={studio.ask} disabled={busy || connection !== "open" || aiOff} onOpenPalette={onOpenPalette} />
          ) : (
            <div className="space-y-6">
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
        draft={studio.draft}
        placeholder={aiOff ? "AI chat is off: set AIPIPE_TOKEN and restart devpilot-ui. Quick actions and Tools still work." : `Ask anything about ${status?.workspace.name ?? "this repository"}…`}
        onSend={studio.ask}
      />
    </div>
  );
}

function HeaderButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] text-[var(--text-muted)] transition hover:bg-[var(--panel-muted)] hover:text-[var(--text)] disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function Badge({ children, tone }: { children: React.ReactNode; tone?: "live" | "warn" }) {
  const color = tone === "live" ? "bg-cyan-500/20 text-cyan-600 dark:text-cyan-300" : tone === "warn" ? "bg-amber-500/20 text-amber-600 dark:text-amber-300"
    : "bg-[var(--panel-muted)] text-[var(--text-faint)]";
  return <span className={`rounded-full px-1.5 text-[10.5px] font-semibold tabular-nums ${color}`}>{children}</span>;
}

function EmptyState({ name, aiOff, onPick, disabled, onOpenPalette }: {
  name?: string; aiOff: boolean; onPick: (t: string) => void; disabled: boolean; onOpenPalette: () => void;
}) {
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
        DevPilot reads, searches and inspects the code through its MCP tools. Each answer shows the steps it took.
      </p>
      <p className="mx-auto mt-2 max-w-md text-[13px] text-[var(--text-faint)]">
        It reads your code freely, and never changes a file or runs tests without your approval.
      </p>
      {aiOff ? (
        <div className="glass mx-auto mt-8 max-w-md rounded-2xl p-4 text-left text-[13px]">
          <div className="mb-1 flex items-center gap-2 font-semibold"><KeyRound size={15} className="text-amber-400" /> Turn on AI chat</div>
          <div className="text-[var(--text-muted)]">
            Get a token at <span className="font-mono">aipipe.org/login</span>, put <span className="font-mono">AIPIPE_TOKEN=…</span> in your
            <span className="font-mono"> .env</span>, then restart <span className="font-mono">devpilot-ui</span>. Until then, use the quick actions,
            the file explorer, or <button onClick={onOpenPalette} className="text-indigo-500 underline">run any tool</button>.
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
      <div className="mt-6 text-[12px] text-[var(--text-faint)]">
        Tip: press <kbd className="rounded border border-[var(--border)] px-1">Ctrl</kbd> + <kbd className="rounded border border-[var(--border)] px-1">K</kbd> to run any of the 18 tools directly.
      </div>
    </motion.div>
  );
}

const enter = { initial: { opacity: 0, y: 14 }, animate: { opacity: 1, y: 0 }, transition: { type: "spring" as const, stiffness: 260, damping: 26 } };

function MessageView({ message, calls, onOpenCall, onOpenFile }: {
  message: ChatMessage; calls: Record<string, ToolCall>; onOpenCall: (id: string) => void; onOpenFile: (p: string) => void;
}) {
  const own = message.callIds.map((id) => calls[id]).filter(Boolean);

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
      <motion.div {...enter} className="glass overflow-hidden rounded-2xl">
        <div className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-2.5 text-[13px]">
          <Zap size={15} className="text-cyan-400" />
          <span className="font-semibold">{message.text}</span>
          {call && <span className="font-mono text-[11px] text-[var(--text-faint)]">{call.name}</span>}
          <span className="ml-auto flex items-center gap-2">
            {call && call.status !== "running" && call.status !== "awaiting" && (
              <button onClick={() => onOpenCall(call.id)} title="Open full result" className="rounded-md p-1 text-[var(--text-faint)] hover:bg-[var(--panel-muted)] hover:text-[var(--text)]">
                <Maximize2 size={13} />
              </button>
            )}
            {call && <StatusIcon status={call.status} size={16} />}
          </span>
        </div>
        <div className="px-4 py-3">
          {!call ? <div className="shimmer h-1 rounded-full" />
            : call.status === "awaiting" ? <div className="text-[12.5px] text-amber-600 dark:text-amber-300">Waiting for your approval…</div>
            : <ResultView call={call} compact />}
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div {...enter} className="flex gap-3">
      <div className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500/30 to-cyan-500/25 text-indigo-400">
        <Bot size={16} />
      </div>
      <div className="min-w-0 flex-1">
        <Steps calls={own} working={Boolean(message.streaming)} onOpen={onOpenCall} />
        {message.text ? (
          <div className={message.streaming ? "caret" : ""}>
            <Markdown text={message.text} onOpenFile={onOpenFile} />
          </div>
        ) : message.streaming ? (
          <ThinkingDots label={own.length ? `${TOOL_LABEL[own[own.length - 1].name] ?? "Using tools"}…` : "Thinking…"} />
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

function Composer({ busy, disabled, placeholder, draft, onSend }: {
  busy: boolean; disabled: boolean; placeholder: string; draft: { text: string; nonce: number } | null; onSend: (t: string) => void;
}) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    requestAnimationFrame(() => {
      area.current?.focus();
      area.current?.setSelectionRange(draft.text.length, draft.text.length);
    });
  }, [draft]);

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
