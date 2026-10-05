export type Access = "read" | "write" | "execute";
export type CallStatus = "running" | "awaiting" | "ok" | "error" | "blocked" | "denied";

export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  anyOf?: JsonSchema[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
}

export interface ToolInfo {
  name: string;
  summary: string;
  description: string;
  access: Access;
  input_schema: JsonSchema;
}

export interface QueryResult {
  status: "ok" | "error" | "blocked" | "refused";
  text: string;
  structured: Record<string, any> | null;
}

export interface GitInfo {
  available: boolean;
  reason?: string;
  branch?: string | null;
  clean?: boolean;
  head?: string;
  counts?: Record<string, number>;
}

export interface Status {
  version: string;
  workspace: { name: string; path: string };
  git: GitInfo;
  tools: ToolInfo[];
  ai: { configured: boolean; model: string; provider: string | null };
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown> | null;
  access: Access;
  source: "agent" | "user";
  status: CallStatus;
  startedAt: number;
  durationMs?: number;
  text?: string;
  structured?: Record<string, unknown> | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "action" | "error";
  text: string;
  callIds: string[];
  streaming?: boolean;
}

export interface ApprovalRequest {
  callId: string;
  name: string;
  access: Access;
  arguments: Record<string, unknown> | null;
}

export type ServerEvent =
  | { type: "assistant_delta"; text: string }
  | { type: "tool_started"; call_id: string; name: string; arguments: Record<string, unknown> | null; access: Access; source: "agent" | "user" }
  | { type: "approval_required"; call_id: string; name: string; arguments: Record<string, unknown> | null; access: Access }
  | { type: "tool_finished"; call_id: string; name: string; status: CallStatus; duration_ms: number; text: string; structured: Record<string, unknown> | null }
  | { type: "answer_done" }
  | { type: "action_done" }
  | { type: "reset_done" }
  | { type: "error"; message: string; refused?: boolean }
  | ({ type: "query_result"; request_id: string } & QueryResult);
