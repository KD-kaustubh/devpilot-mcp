import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityPanel } from "./components/Activity";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { ChatPanel } from "./components/Chat";
import { ResultDrawer } from "./components/ResultDrawer";
import { Sidebar } from "./components/Sidebar";
import { useTheme } from "./lib";
import { useStudio } from "./useStudio";

export default function App() {
  const studio = useStudio();
  const [dark, toggleTheme] = useTheme();
  const [openCallId, setOpenCallId] = useState<string | null>(null);
  const [activityOpen, setActivityOpen] = useState(false);
  const pendingFile = useRef<string | null>(null);

  // Clicking a file path in an answer reads it with read_file, then opens the result.
  const openFile = useCallback((path: string) => {
    if (studio.busy) return;
    pendingFile.current = path;
    studio.runTool("read_file", { path }, `Open ${path}`);
  }, [studio]);

  useEffect(() => {
    const path = pendingFile.current;
    if (!path) return;
    const call = [...studio.order].reverse().map((id) => studio.calls[id])
      .find((c) => c?.name === "read_file" && c.source === "user" && c.arguments?.path === path);
    if (call && call.status !== "running") {
      pendingFile.current = null;
      setOpenCallId(call.id);
    }
  }, [studio.calls, studio.order]);

  const activity = (
    <ActivityPanel calls={studio.calls} order={studio.order} onOpen={setOpenCallId} onClear={studio.clearActivity} />
  );

  return (
    <div className="studio-bg relative h-full overflow-hidden">
      <div className="studio-grid pointer-events-none absolute inset-0 opacity-60" />
      <div className="relative grid h-full grid-cols-1 lg:grid-cols-[292px_minmax(0,1fr)] xl:grid-cols-[292px_minmax(0,1fr)_360px]">
        <div className="hidden min-h-0 border-r border-[var(--border)] bg-[var(--panel)] backdrop-blur-xl lg:block">
          <Sidebar studio={studio} dark={dark} toggleTheme={toggleTheme} />
        </div>
        <main className="min-h-0 min-w-0">
          <ChatPanel
            studio={studio}
            onOpenCall={setOpenCallId}
            onOpenFile={openFile}
            onToggleActivity={() => setActivityOpen((o) => !o)}
          />
        </main>
        <div className="hidden min-h-0 border-l border-[var(--border)] bg-[var(--panel)] backdrop-blur-xl xl:block">{activity}</div>
      </div>

      {/* Below xl the activity panel slides in over the chat. */}
      <AnimatePresence>
        {activityOpen && (
          <motion.div
            className="fixed inset-y-0 right-0 z-30 w-[360px] max-w-full border-l border-[var(--border)] bg-[var(--panel-solid)] shadow-2xl xl:hidden"
            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 280, damping: 32 }}
          >
            {activity}
          </motion.div>
        )}
      </AnimatePresence>

      <ApprovalDialog request={studio.approval} workspace={studio.status?.workspace.name} onAnswer={studio.answerApproval} />
      <ResultDrawer call={openCallId ? studio.calls[openCallId] ?? null : null} onClose={() => setOpenCallId(null)} />
    </div>
  );
}
