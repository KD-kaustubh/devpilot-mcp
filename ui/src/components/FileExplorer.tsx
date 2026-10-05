import { AnimatePresence, motion } from "framer-motion";
import { ChevronRight, File, FileCode2, FileText, Folder, FolderOpen, Lock, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { QueryResult } from "../types";

interface Entry { name: string; path: string; type: "file" | "directory" | "other"; size_bytes: number | null }
interface Node { entries?: Entry[]; loading?: boolean; error?: string; truncated?: boolean }

const HIDDEN = new Set([".git"]);
const CODE = /\.(py|pyi|ts|tsx|js|jsx|mjs|json|toml|ya?ml|go|rs|java|c|h|cpp|cs|rb|php|sh|css|html|sql)$/i;
const SECRET = /^(\.env(\..+)?|id_(rsa|dsa|ecdsa|ed25519).*|.*\.(pem|key|p12|pfx)|credentials\.json|\.npmrc|\.pypirc|\.netrc)$/i;

function fileIcon(name: string) {
  if (SECRET.test(name) && !/\.(example|sample|template)$/i.test(name)) return <Lock size={13} className="text-rose-400" />;
  if (CODE.test(name)) return <FileCode2 size={13} className="text-indigo-400" />;
  if (/\.(md|txt|rst)$/i.test(name)) return <FileText size={13} className="text-[var(--text-faint)]" />;
  return <File size={13} className="text-[var(--text-faint)]" />;
}

/** A lazy folder tree built from silent list_directory queries (nothing appears in the chat). */
export function FileExplorer({ query, onOpenFile, connected }: {
  query: (name: string, args: Record<string, unknown>) => Promise<QueryResult>;
  onOpenFile: (path: string) => void;
  connected: boolean;
}) {
  const [nodes, setNodes] = useState<Record<string, Node>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["."]));

  const load = useCallback(async (path: string) => {
    setNodes((n) => ({ ...n, [path]: { ...n[path], loading: true, error: undefined } }));
    const result = await query("list_directory", { path });
    setNodes((n) => ({
      ...n,
      [path]: result.status === "ok"
        ? { entries: (result.structured?.entries ?? []) as Entry[], truncated: result.structured?.truncated }
        : { error: result.text.replace(/^Error executing tool \w+: /, "") },
    }));
  }, [query]);

  useEffect(() => {
    if (connected && !nodes["."]) load(".");
  }, [connected, load, nodes]);

  const toggle = (path: string) => {
    const opening = !expanded.has(path);
    setExpanded((old) => {
      const next = new Set(old);
      if (opening) next.add(path);
      else next.delete(path);
      return next;
    });
    if (opening && !nodes[path]?.entries) load(path);
  };

  const refresh = () => {
    setNodes({});
    setExpanded(new Set(["."]));
    load(".");
  };

  const renderDir = (path: string, depth: number): React.ReactNode => {
    const node = nodes[path];
    if (!node || node.loading) {
      return <div className="py-1 text-[11.5px] text-[var(--text-faint)]" style={{ paddingLeft: depth * 14 + 22 }}>loading…</div>;
    }
    if (node.error) return <div className="py-1 text-[11.5px] text-rose-500" style={{ paddingLeft: depth * 14 + 22 }}>{node.error}</div>;
    const entries = (node.entries ?? []).filter((e) => !HIDDEN.has(e.name));
    if (entries.length === 0) return <div className="py-1 text-[11.5px] text-[var(--text-faint)]" style={{ paddingLeft: depth * 14 + 22 }}>empty</div>;
    return entries.map((entry) => {
      const isDir = entry.type === "directory";
      const isOpen = expanded.has(entry.path);
      return (
        <div key={entry.path}>
          <button
            onClick={() => (isDir ? toggle(entry.path) : onOpenFile(entry.path))}
            title={entry.path}
            className="flex w-full items-center gap-1.5 rounded-md py-[3px] pr-2 text-left text-[12.5px] text-[var(--text-muted)] transition hover:bg-indigo-500/8 hover:text-[var(--text)]"
            style={{ paddingLeft: depth * 14 + 4 }}
          >
            {isDir ? (
              <>
                <motion.span animate={{ rotate: isOpen ? 90 : 0 }} className="text-[var(--text-faint)]"><ChevronRight size={13} /></motion.span>
                {isOpen ? <FolderOpen size={14} className="text-indigo-400" /> : <Folder size={14} className="text-indigo-400" />}
              </>
            ) : (
              <span className="ml-[17px]">{fileIcon(entry.name)}</span>
            )}
            <span className="truncate">{entry.name}</span>
          </button>
          <AnimatePresence initial={false}>
            {isDir && isOpen && (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
                {renderDir(entry.path, depth + 1)}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      );
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text-faint)]">Files</span>
        <button onClick={refresh} title="Refresh" className="rounded-md p-1 text-[var(--text-faint)] hover:bg-[var(--panel-muted)] hover:text-[var(--text)]">
          <RefreshCw size={13} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pr-1 scroll-slim">{renderDir(".", 0)}</div>
      <div className="pt-2 text-[11px] text-[var(--text-faint)]">
        <Lock size={10} className="mr-1 inline text-rose-400" />secret files are listed but never opened
      </div>
    </div>
  );
}
