import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { useEffect } from "react";

/** A right-hand panel that slides over the app (activity, changes, file viewer, result details). */
export function SlideOver({ open, onClose, title, icon, actions, width = "max-w-2xl", children }: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  icon?: React.ReactNode;
  actions?: React.ReactNode;
  width?: string;
  children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      // The approval dialog handles Escape itself while it is open.
      if (e.key === "Escape" && !document.querySelector("[role=dialog][aria-modal=true]")) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 z-40 bg-slate-950/40 backdrop-blur-[2px]"
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className={`fixed inset-y-0 right-0 z-40 flex w-full ${width} flex-col border-l border-[var(--border)] bg-[var(--panel-solid)] shadow-2xl`}
            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 280, damping: 32 }}
          >
            <div className="flex items-center gap-3 border-b border-[var(--border)] px-5 py-3.5">
              {icon}
              <div className="min-w-0 flex-1">{title}</div>
              {actions}
              <button onClick={onClose} className="rounded-lg p-2 text-[var(--text-muted)] hover:bg-[var(--panel-muted)]" title="Close (Esc)">
                <X size={16} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto scroll-slim">{children}</div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}
