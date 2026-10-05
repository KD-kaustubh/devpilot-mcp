import { Activity } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityPanel } from "./components/Activity";
import { ApprovalDialog } from "./components/ApprovalDialog";
import { ChangesPanel } from "./components/ChangesPanel";
import { ChatPanel } from "./components/Chat";
import { CommandPalette } from "./components/CommandPalette";
import { FileViewer } from "./components/FileViewer";
import { ResultDrawer } from "./components/ResultDrawer";
import { ResultActionsContext, type ResultActions } from "./components/results/parts";
import { Sidebar } from "./components/Sidebar";
import { SlideOver } from "./components/SlideOver";
import { useTheme } from "./lib";
import { useStudio } from "./useStudio";

type Panel = "activity" | "changes" | null;

export default function App() {
  const studio = useStudio();
  const [dark, toggleTheme] = useTheme();
  const [openCallId, setOpenCallId] = useState<string | null>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [file, setFile] = useState<{ path: string; line?: number } | null>(null);
  const [palette, setPalette] = useState<{ open: boolean; tool: string | null }>({ open: false, tool: null });

  const openFile = useCallback((path: string, line?: number) => {
    setOpenCallId(null);
    setFile({ path, line });
  }, []);

  const undo = useCallback((changeId: string) => {
    if (!studio.busy) studio.runTool("revert_patch", { change_id: changeId }, "Undo change");
  }, [studio]);

  const actions = useMemo<ResultActions>(() => ({ openFile, openDetails: setOpenCallId, undo }), [openFile, undo]);

  // Ctrl/⌘+K opens the tool runner from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette((p) => ({ open: !p.open, tool: null }));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const askAboutFile = (path: string) => {
    setFile(null);
    studio.prefill(`About \`${path}\`: `);
  };

  return (
    <ResultActionsContext.Provider value={actions}>
      <div className="studio-bg relative h-full overflow-hidden">
        <div className="studio-grid pointer-events-none absolute inset-0 opacity-60" />
        <div className="relative grid h-full grid-cols-1 lg:grid-cols-[292px_minmax(0,1fr)]">
          <div className="hidden min-h-0 border-r border-[var(--border)] bg-[var(--panel)] backdrop-blur-xl lg:block">
            <Sidebar
              studio={studio}
              dark={dark}
              toggleTheme={toggleTheme}
              onOpenTool={(tool) => setPalette({ open: true, tool })}
              onOpenFile={openFile}
            />
          </div>
          <main className="min-h-0 min-w-0">
            <ChatPanel
              studio={studio}
              onOpenCall={setOpenCallId}
              onOpenFile={openFile}
              onOpenActivity={() => setPanel("activity")}
              onOpenChanges={() => setPanel("changes")}
              onOpenPalette={() => setPalette({ open: true, tool: null })}
            />
          </main>
        </div>

        <SlideOver
          open={panel === "activity"}
          onClose={() => setPanel(null)}
          width="max-w-md"
          icon={<Activity size={18} className="text-cyan-400" />}
          title={<div className="font-semibold">Activity history</div>}
        >
          <ActivityPanel calls={studio.calls} order={studio.order} onOpen={setOpenCallId} onClear={studio.clearActivity} />
        </SlideOver>

        <ChangesPanel
          open={panel === "changes"}
          onClose={() => setPanel(null)}
          changes={studio.changes}
          busy={studio.busy}
          onUndo={undo}
          query={studio.query}
        />

        <FileViewer target={file} onClose={() => setFile(null)} query={studio.query} onAsk={askAboutFile}
          canAsk={Boolean(studio.status?.ai.configured)} />
        <ResultDrawer call={openCallId ? studio.calls[openCallId] ?? null : null} onClose={() => setOpenCallId(null)} />

        <CommandPalette
          open={palette.open}
          tools={studio.status?.tools ?? []}
          initialTool={palette.tool}
          disabled={studio.busy || studio.connection !== "open"}
          onClose={() => setPalette({ open: false, tool: null })}
          onRun={studio.runTool}
        />
        <ApprovalDialog request={studio.approval} workspace={studio.status?.workspace.name} onAnswer={studio.answerApproval} />
      </div>
    </ResultActionsContext.Provider>
  );
}
