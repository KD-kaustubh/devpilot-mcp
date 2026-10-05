import type { JsonSchema } from "../types";

/** One input field described by a JSON schema property (anyOf with null = optional/nullable). */
interface Field {
  name: string;
  label: string;
  description?: string;
  kind: "text" | "textarea" | "number" | "boolean" | "select";
  options?: string[];
  min?: number;
  max?: number;
  required: boolean;
  defaultValue: unknown;
}

function effective(schema: JsonSchema): { schema: JsonSchema; nullable: boolean } {
  if (!schema.anyOf) return { schema, nullable: false };
  const branches = schema.anyOf.filter((s) => s.type !== "null");
  return { schema: { ...branches[0], default: schema.default, description: schema.description ?? branches[0]?.description }, nullable: branches.length < schema.anyOf.length };
}

export function fieldsOf(inputSchema: JsonSchema): Field[] {
  const required = new Set(inputSchema.required ?? []);
  return Object.entries(inputSchema.properties ?? {}).map(([name, raw]) => {
    const { schema } = effective(raw);
    const type = Array.isArray(schema.type) ? schema.type[0] : schema.type;
    const enumValues = schema.enum ?? (schema.const !== undefined ? [schema.const] : undefined);
    const kind: Field["kind"] = enumValues ? "select" : type === "boolean" ? "boolean"
      : type === "integer" || type === "number" ? "number" : name === "patch" ? "textarea" : "text";
    return {
      name,
      label: raw.title ?? schema.title ?? name.replace(/_/g, " "),
      description: raw.description ?? schema.description,
      kind,
      options: enumValues?.map(String),
      min: schema.minimum,
      max: schema.maximum,
      required: required.has(name),
      defaultValue: raw.default ?? schema.default,
    };
  });
}

export function initialValues(fields: Field[]): Record<string, string | boolean> {
  return Object.fromEntries(fields.map((f) => [f.name,
    f.kind === "boolean" ? Boolean(f.defaultValue) : f.defaultValue == null ? "" : String(f.defaultValue)]));
}

/** Turn form values into tool arguments: optional empty fields are left out so the tool's defaults apply. */
export function toArguments(fields: Field[], values: Record<string, string | boolean>): { args: Record<string, unknown>; missing: string[] } {
  const args: Record<string, unknown> = {};
  const missing: string[] = [];
  for (const f of fields) {
    const value = values[f.name];
    if (f.kind === "boolean") {
      args[f.name] = Boolean(value);
      continue;
    }
    const text = String(value ?? "");
    if (!text.trim()) {
      if (f.required) missing.push(f.label);
      continue;
    }
    args[f.name] = f.kind === "number" ? Number(text) : text;
  }
  return { args, missing };
}

const inputClass = "w-full rounded-lg border border-[var(--border)] bg-[var(--panel-muted)]/60 px-3 py-2 text-[13px] outline-none transition focus:border-indigo-400/60";

export function SchemaForm({ fields, values, onChange, onSubmit }: {
  fields: Field[]; values: Record<string, string | boolean>; onChange: (name: string, value: string | boolean) => void; onSubmit: () => void;
}) {
  if (fields.length === 0) return <div className="text-[13px] text-[var(--text-muted)]">This tool takes no inputs.</div>;
  return (
    <form className="space-y-3.5" onSubmit={(e) => { e.preventDefault(); onSubmit(); }}>
      {fields.map((f, i) => (
        <label key={f.name} className="block space-y-1.5">
          <span className="flex items-center gap-1.5 text-[12.5px] font-medium">
            {f.label}
            {f.required ? <span className="text-rose-400">*</span> : <span className="text-[11px] font-normal text-[var(--text-faint)]">optional</span>}
            {f.kind === "number" && (f.min !== undefined || f.max !== undefined) && (
              <span className="text-[11px] font-normal text-[var(--text-faint)]">{f.min ?? "…"}–{f.max ?? "…"}</span>
            )}
          </span>
          {f.kind === "boolean" ? (
            <button
              type="button"
              role="switch"
              aria-checked={Boolean(values[f.name])}
              onClick={() => onChange(f.name, !values[f.name])}
              className={`relative h-6 w-11 rounded-full transition ${values[f.name] ? "bg-indigo-500" : "bg-[var(--panel-muted)] ring-1 ring-[var(--border)]"}`}
            >
              <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${values[f.name] ? "left-[22px]" : "left-0.5"}`} />
            </button>
          ) : f.kind === "select" ? (
            <select value={String(values[f.name])} onChange={(e) => onChange(f.name, e.target.value)} className={inputClass}>
              {!f.required && <option value="">default</option>}
              {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : f.kind === "textarea" ? (
            <textarea
              autoFocus={i === 0}
              rows={8}
              value={String(values[f.name])}
              onChange={(e) => onChange(f.name, e.target.value)}
              placeholder={"--- a/path/to/file\n+++ b/path/to/file\n@@ -1,1 +1,1 @@\n-old line\n+new line"}
              className={`${inputClass} resize-y font-mono text-[12px]`}
            />
          ) : (
            <input
              autoFocus={i === 0}
              type={f.kind === "number" ? "number" : "text"}
              min={f.min}
              max={f.max}
              value={String(values[f.name])}
              onChange={(e) => onChange(f.name, e.target.value)}
              placeholder={f.name === "path" ? "." : ""}
              className={`${inputClass} ${f.name === "path" || f.name === "change_id" ? "font-mono" : ""}`}
            />
          )}
          {f.description && <span className="block text-[11.5px] text-[var(--text-faint)]">{f.description}</span>}
        </label>
      ))}
      <button type="submit" className="hidden" />
    </form>
  );
}
