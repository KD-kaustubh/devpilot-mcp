import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { ApprovalRequest, ChatMessage, QueryResult, ServerEvent, Status, ToolCall } from "./types";

type Connection = "connecting" | "open" | "closed";

interface State {
  messages: ChatMessage[];
  calls: Record<string, ToolCall>;
  order: string[]; // call ids, oldest first
  approval: ApprovalRequest | null;
  busy: boolean;
  activeAssistant: string | null; // the assistant message currently being written
  activeAction: string | null; // the quick-action message currently running
}

type Action =
  | { kind: "event"; event: ServerEvent }
  | { kind: "ask"; text: string }
  | { kind: "action"; label: string }
  | { kind: "reset" }
  | { kind: "clearActivity" }
  | { kind: "resolveApproval" }
  | { kind: "disconnected" };

const initial: State = { messages: [], calls: {}, order: [], approval: null, busy: false, activeAssistant: null, activeAction: null };

let counter = 0;
const uid = () => `m${Date.now().toString(36)}${(counter++).toString(36)}`;

function attach(messages: ChatMessage[], messageId: string | null, callId: string): ChatMessage[] {
  if (!messageId) return messages;
  return messages.map((m) => (m.id === messageId ? { ...m, callIds: [...m.callIds, callId] } : m));
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "ask": {
      const user: ChatMessage = { id: uid(), role: "user", text: action.text, callIds: [] };
      const assistant: ChatMessage = { id: uid(), role: "assistant", text: "", callIds: [], streaming: true };
      return { ...state, messages: [...state.messages, user, assistant], busy: true, activeAssistant: assistant.id };
    }
    case "action": {
      const message: ChatMessage = { id: uid(), role: "action", text: action.label, callIds: [], streaming: true };
      return { ...state, messages: [...state.messages, message], busy: true, activeAction: message.id };
    }
    case "reset":
      return { ...initial, calls: state.calls, order: state.order };
    case "clearActivity":
      return { ...state, calls: {}, order: [] };
    case "resolveApproval":
      return { ...state, approval: null };
    case "disconnected":
      return { ...state, busy: false, approval: null, activeAssistant: null, activeAction: null,
        messages: state.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)) };
    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "assistant_delta":
      return {
        ...state,
        messages: state.messages.map((m) => (m.id === state.activeAssistant ? { ...m, text: m.text + event.text } : m)),
      };
    case "tool_started": {
      const call: ToolCall = {
        id: event.call_id, name: event.name, arguments: event.arguments, access: event.access,
        source: event.source, status: "running", startedAt: Date.now(),
      };
      const owner = event.source === "agent" ? state.activeAssistant : state.activeAction;
      return { ...state, calls: { ...state.calls, [call.id]: call }, order: [...state.order, call.id],
        messages: attach(state.messages, owner, call.id) };
    }
    case "approval_required": {
      const existing = state.calls[event.call_id];
      const calls = existing ? { ...state.calls, [event.call_id]: { ...existing, status: "awaiting" as const } } : state.calls;
      return { ...state, calls, approval: { callId: event.call_id, name: event.name, access: event.access, arguments: event.arguments } };
    }
    case "tool_finished": {
      const existing = state.calls[event.call_id];
      if (!existing) return state;
      const call: ToolCall = { ...existing, status: event.status, durationMs: event.duration_ms, text: event.text, structured: event.structured };
      return { ...state, calls: { ...state.calls, [call.id]: call },
        approval: state.approval?.callId === call.id ? null : state.approval };
    }
    case "answer_done":
      return { ...state, busy: false, activeAssistant: null,
        messages: state.messages.map((m) => (m.id === state.activeAssistant ? { ...m, streaming: false } : m)) };
    case "action_done":
      return { ...state, busy: false, activeAction: null,
        messages: state.messages.map((m) => (m.id === state.activeAction ? { ...m, streaming: false } : m)) };
    case "reset_done":
    case "query_result":
      return state;
    case "error": {
      const error: ChatMessage = { id: uid(), role: "error", text: event.message, callIds: [] };
      if (!event.refused) return { ...state, messages: [...state.messages, error] }; // answer_done follows
      // The request never started: drop its empty placeholder and stop waiting.
      const pending = new Set([state.activeAssistant, state.activeAction]);
      return { ...state, busy: false, activeAssistant: null, activeAction: null,
        messages: [...state.messages.filter((m) => !(pending.has(m.id) && m.callIds.length === 0)), error] };
    }
  }
}

export function useStudio() {
  const [state, dispatch] = useReducer(reducer, initial);
  const [status, setStatus] = useState<Status | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const socket = useRef<WebSocket | null>(null);
  const queries = useRef(new Map<string, (result: QueryResult) => void>());
  const [draft, setDraft] = useState<{ text: string; nonce: number } | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const response = await fetch("./api/status");
      if (response.ok) setStatus(await response.json());
    } catch {
      /* the backend is restarting; the socket reconnect will refresh again */
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let retry = 0;
    let timer: number | undefined;

    const connect = () => {
      setConnection("connecting");
      const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
      const ws = new WebSocket(url);
      socket.current = ws;
      ws.onopen = () => {
        retry = 0;
        setConnection("open");
        refreshStatus();
      };
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data) as ServerEvent;
        if (event.type === "query_result") {
          queries.current.get(event.request_id)?.(event);
          queries.current.delete(event.request_id);
          return;
        }
        dispatch({ kind: "event", event });
        if (event.type === "answer_done" || event.type === "action_done") refreshStatus();
      };
      ws.onclose = () => {
        socket.current = null;
        for (const resolve of queries.current.values()) resolve({ status: "error", text: "Disconnected from DevPilot.", structured: null });
        queries.current.clear();
        dispatch({ kind: "disconnected" });
        if (stopped) return;
        setConnection("closed");
        timer = window.setTimeout(connect, Math.min(8000, 600 * 2 ** retry++));
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket.current?.close();
    };
  }, [refreshStatus]);

  const send = useCallback((payload: Record<string, unknown>) => {
    if (socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }, []);

  const ask = useCallback((text: string) => {
    if (send({ type: "ask", text })) dispatch({ kind: "ask", text });
  }, [send]);

  const runTool = useCallback((name: string, args: Record<string, unknown>, label: string) => {
    if (send({ type: "run_tool", name, arguments: args })) dispatch({ kind: "action", label });
  }, [send]);

  const answerApproval = useCallback((callId: string, approved: boolean) => {
    send({ type: "approval", call_id: callId, approved });
    dispatch({ kind: "resolveApproval" });
  }, [send]);

  const reset = useCallback(() => {
    send({ type: "reset" });
    dispatch({ kind: "reset" });
  }, [send]);

  const clearActivity = useCallback(() => dispatch({ kind: "clearActivity" }), []);

  /** A silent read-only lookup (no chat message, no timeline card). The server refuses anything else. */
  const query = useCallback((name: string, args: Record<string, unknown>) => new Promise<QueryResult>((resolve) => {
    const requestId = `q${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
    queries.current.set(requestId, resolve);
    if (!send({ type: "query", request_id: requestId, name, arguments: args })) {
      queries.current.delete(requestId);
      resolve({ status: "error", text: "Not connected to DevPilot.", structured: null });
    }
  }), [send]);

  /** Put text into the composer (e.g. "Ask about this file"). */
  const prefill = useCallback((text: string) => setDraft({ text, nonce: Date.now() }), []);

  /** Every successful apply_patch this session, with whether a later revert_patch undid it. */
  const changes = useMemo(() => {
    const all = state.order.map((id) => state.calls[id]).filter(Boolean);
    const reverted = new Set(all.filter((c) => c.name === "revert_patch" && c.status === "ok").map((c) => String(c.structured?.change_id)));
    return all.filter((c) => c.name === "apply_patch" && c.status === "ok" && c.structured?.change_id)
      .map((call) => ({ call, changeId: String(call.structured!.change_id), undone: reverted.has(String(call.structured!.change_id)) }))
      .reverse();
  }, [state.calls, state.order]);

  return { ...state, status, connection, ask, runTool, answerApproval, reset, clearActivity, refreshStatus, query, draft, prefill, changes };
}

export type Studio = ReturnType<typeof useStudio>;
