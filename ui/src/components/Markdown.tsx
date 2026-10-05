import { memo } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

// Inline code that looks like a repository file: `src/app.py`, `docs/USAGE.md:12`, `README.md`.
const PATH_WITH_DIR = /^[\w.-]+(\/[\w.-]+)+\.[A-Za-z0-9]{1,8}(:\d+)?$/;
const BARE_FILE = /^[\w.-]+\.(py|pyi|ts|tsx|js|jsx|md|json|toml|ya?ml|txt|cfg|ini|rs|go|java|html|css)(:\d+)?$/;

function isFileRef(value: string): boolean {
  return PATH_WITH_DIR.test(value) || BARE_FILE.test(value);
}

export const Markdown = memo(function Markdown({ text, onOpenFile }: { text: string; onOpenFile: (path: string) => void }) {
  return (
    <div className="prose-studio">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>
          ),
          code: ({ className, children }) => {
            const value = String(children ?? "");
            const inline = !className && !value.includes("\n");
            if (inline && isFileRef(value)) {
              const path = value.replace(/:\d+$/, "");
              return (
                <button
                  onClick={() => onOpenFile(path)}
                  title={`Open ${path}`}
                  className="rounded-md border border-indigo-400/30 bg-indigo-500/10 px-1.5 py-px font-mono text-[0.82em] text-indigo-600 transition hover:border-cyan-400/50 hover:text-cyan-600 dark:text-indigo-300 dark:hover:text-cyan-300"
                >
                  {value}
                </button>
              );
            }
            return <code className={className}>{children}</code>;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
