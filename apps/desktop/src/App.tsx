import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, ArrowUpRight, Bot, Boxes, Check, ChevronDown, CircleHelp, Gauge,
  HardDrive, LayoutDashboard, ListTodo, LockKeyhole, Menu, MoreHorizontal,
  RefreshCw, Settings2, ShieldCheck, Sun, Moon, Workflow, X,
} from "lucide-react";

type Section = "Overview" | "Executions" | "MCP servers" | "Approvals" | "Settings";
type ConnectionState = "checking" | "connected" | "disconnected" | "recovering" | "error";
type DetailState = "idle" | "loading" | "ready" | "error" | "missing";
type StreamState = "idle" | "connecting" | "live" | "reconnecting" | "unavailable";

type TaskEventView = {
  id: number;
  type: string;
  timestamp: string;
  payload: unknown;
};

const taskIDPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const maxVisibleEvents = 200;

const navItems: { label: Section; icon: typeof LayoutDashboard; detail: string }[] = [
  { label: "Overview", icon: LayoutDashboard, detail: "Runtime pulse and persisted activity" },
  { label: "Executions", icon: ListTodo, detail: "Persisted task history and lifecycle status" },
  { label: "MCP servers", icon: Boxes, detail: "Tool server visibility is not exposed by the local API" },
  { label: "Approvals", icon: ShieldCheck, detail: "Approval visibility is not exposed by the local API" },
  { label: "Settings", icon: Settings2, detail: "Desktop client and local connection boundaries" },
];

const connectionLabels: Record<ConnectionState, string> = {
  checking: "Connecting to runtime",
  connected: "Runtime connected",
  disconnected: "Runtime disconnected",
  recovering: "Reconnecting to runtime",
  error: "Runtime error",
};

const terminalStatuses = new Set(["succeeded", "failed", "cancelled"]);

function statusTone(status: string): string {
  if (status === "succeeded") return "done";
  if (status === "failed") return "error";
  if (status === "waiting_approval") return "warn";
  if (terminalStatuses.has(status)) return "neutral";
  return "active";
}

function formatTime(value?: string): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "Not provided";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function suggestedTaskID(): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `task-${Date.now()}-${random}`;
}

function toEventView(event: CraneTaskEvent): TaskEventView | null {
  if (event.event === "snapshot") return null;
  const data = asRecord(event.data);
  const id = Number(event.id || data?.id);
  const type = typeof data?.type === "string" ? data.type : event.event;
  if (!Number.isSafeInteger(id) || id <= 0 || !type) return null;
  return {
    id,
    type,
    timestamp: typeof data?.timestamp === "string" ? data.timestamp : "",
    payload: data?.payload ?? null,
  };
}

function eventTerminalStatus(event: TaskEventView): string | null {
  const payload = asRecord(event.payload);
  const status = payload?.status;
  return typeof status === "string" && terminalStatuses.has(status) ? status : null;
}

function streamLabel(state: StreamState): string {
  if (state === "live") return "LIVE";
  if (state === "connecting") return "CONNECTING";
  if (state === "reconnecting") return "RECONNECTING";
  if (state === "unavailable") return "UNAVAILABLE";
  return "IDLE";
}

function App() {
  const [section, setSection] = useState<Section>("Overview");
  const [appInfo, setAppInfo] = useState<CraneAppInfo | null>(null);
  const [health, setHealth] = useState<CraneHealth | null>(null);
  const [tasks, setTasks] = useState<CraneTask[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [connectionMessage, setConnectionMessage] = useState("Connecting to the local supervisor…");
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CraneTask | null>(null);
  const [detailState, setDetailState] = useState<DetailState>("idle");
  const [detailMessage, setDetailMessage] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerID, setComposerID] = useState("");
  const [composerInput, setComposerInput] = useState("");
  const [composerError, setComposerError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [taskEvents, setTaskEvents] = useState<TaskEventView[]>([]);
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [streamMessage, setStreamMessage] = useState("");

  const activeStreamRef = useRef<CraneEventSubscription | null>(null);
  const streamTokenRef = useRef(0);
  const eventCursorRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);

  const bridgeAvailable = Boolean(window.craneDesktop);
  const active = navItems.find((item) => item.label === section) ?? navItems[0];

  const refresh = useCallback(async (mode: "initial" | "manual" = "manual") => {
    const bridge = window.craneDesktop;
    if (!bridge) {
      setConnection("disconnected");
      setConnectionMessage("The restricted desktop bridge is unavailable.");
      return;
    }
    setBusy(true);
    setConnection((current) => mode === "manual" && current === "disconnected" ? "recovering" : current);
    const [healthResult, tasksResult] = await Promise.all([bridge.getHealth(), bridge.getTasks()]);
    const failure = !healthResult.ok ? healthResult : !tasksResult.ok ? tasksResult : null;
    if (failure) {
      setHealth(null);
      setTasks([]);
      setConnection(failure.error.kind === "disconnected" ? "disconnected" : "error");
      setConnectionMessage(failure.error.message);
    } else if (healthResult.ok && tasksResult.ok) {
      setHealth(healthResult.data);
      setTasks(tasksResult.data.tasks);
      setConnection("connected");
      setConnectionMessage("");
      setUpdatedAt(new Date());
    }
    setBusy(false);
  }, []);

  useEffect(() => {
    const bridge = window.craneDesktop;
    if (bridge) void bridge.getAppInfo().then(setAppInfo).catch(() => setAppInfo(null));
  }, []);

  useEffect(() => {
    void refresh("initial");
    const timer = window.setInterval(() => void refresh("manual"), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const loadTaskDetail = useCallback(async (id: string): Promise<boolean> => {
    const bridge = window.craneDesktop;
    if (!bridge) {
      setDetailState("error");
      setDetailMessage("The restricted desktop bridge is unavailable.");
      return false;
    }
    const result = await bridge.getTask(id);
    if (!result.ok) {
      setDetailState(result.error.kind === "not_found" ? "missing" : "error");
      setDetailMessage(result.error.message);
      return false;
    }
    setDetail(result.data);
    setDetailState("ready");
    setDetailMessage("");
    return true;
  }, []);

  const stopTaskEvents = useCallback(() => {
    streamTokenRef.current += 1;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    const current = activeStreamRef.current;
    activeStreamRef.current = null;
    if (current) {
      try { current.unsubscribe(); } catch { /* cleanup is best-effort */ }
    }
    eventCursorRef.current = 0;
    reconnectAttemptRef.current = 0;
    setTaskEvents([]);
    setStreamState("idle");
    setStreamMessage("");
  }, []);

  const handleTaskStreamEvent = useCallback((id: string, event: CraneTaskEvent, token: number) => {
    if (token !== streamTokenRef.current) return;
    if (event.event === "snapshot") {
      const task = asRecord(event.data);
      if (task && task.id === id) {
        setDetail((current) => {
          if (current?.id !== id) return current;
          return {
            ...current,
            status: typeof task.status === "string" ? task.status : current.status,
            result: task.result ?? current.result,
            error: typeof task.error === "string" ? task.error : current.error,
            updated_at: typeof task.updated_at === "string" ? task.updated_at : current.updated_at,
            completed_at: typeof task.completed_at === "string" ? task.completed_at : current.completed_at,
            has_checkpoint: typeof task.has_checkpoint === "boolean" ? task.has_checkpoint : current.has_checkpoint,
          };
        });
      }
      return;
    }
    const view = toEventView(event);
    if (!view) return;
    eventCursorRef.current = Math.max(eventCursorRef.current, view.id);
    setTaskEvents((current) => {
      if (current.some((item) => item.id === view.id)) return current;
      return [...current, view].sort((left, right) => left.id - right.id).slice(-maxVisibleEvents);
    });
    if (view.type === "task_finished" || eventTerminalStatus(view)) {
      void loadTaskDetail(id);
      void refresh("manual");
    }
  }, [loadTaskDetail, refresh]);

  const startTaskEvents = useCallback((id: string) => {
    const bridge = window.craneDesktop;
    const token = streamTokenRef.current + 1;
    streamTokenRef.current = token;
    reconnectAttemptRef.current = 0;
    setStreamState(bridge ? "connecting" : "unavailable");
    setStreamMessage(bridge ? "" : "The restricted desktop bridge is unavailable.");
    if (!bridge) return;

    const releaseStream = () => {
      const current = activeStreamRef.current;
      activeStreamRef.current = null;
      if (current) {
        try { current.unsubscribe(); } catch { /* cleanup is best-effort */ }
      }
    };

    let connect: () => Promise<void>;
    const scheduleReconnect = (message: string) => {
      if (token !== streamTokenRef.current) return;
      releaseStream();
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      const attempt = reconnectAttemptRef.current++;
      const delay = Math.min(5000, 500 * (2 ** attempt));
      setStreamState("reconnecting");
      setStreamMessage(message);
      reconnectTimerRef.current = window.setTimeout(() => void connect(), delay);
    };

    connect = async () => {
      if (token !== streamTokenRef.current) return;
      reconnectTimerRef.current = null;
      setStreamState((current) => current === "reconnecting" ? current : "connecting");
      let result: CraneBridgeResult<CraneEventSubscription>;
      try {
        result = await bridge.subscribeTaskEvents(
          id,
          eventCursorRef.current,
          (event) => handleTaskStreamEvent(id, event, token),
          (error) => scheduleReconnect(error.message || "Event stream closed; task continues under the supervisor."),
        );
      } catch {
        scheduleReconnect("Event stream could not be opened; task continues under the supervisor.");
        return;
      }
      if (token !== streamTokenRef.current) {
        if (result.ok) result.data.unsubscribe();
        return;
      }
      if (!result.ok) {
        if (result.error.kind === "bad_request") {
          setStreamState("unavailable");
          setStreamMessage(result.error.message);
          return;
        }
        scheduleReconnect(result.error.message);
        return;
      }
      activeStreamRef.current = result.data;
      reconnectAttemptRef.current = 0;
      setStreamState("live");
      setStreamMessage("");
    };
    void connect();
  }, [handleTaskStreamEvent]);

  const selectTask = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetail(null);
    setDetailState("loading");
    setDetailMessage("");
    setActionMessage("");
    stopTaskEvents();
    const loaded = await loadTaskDetail(id);
    if (loaded) startTaskEvents(id);
  }, [loadTaskDetail, startTaskEvents, stopTaskEvents]);

  const openComposer = useCallback(() => {
    setComposerID(suggestedTaskID());
    setComposerInput("");
    setComposerError("");
    setComposerOpen(true);
  }, []);

  const submitNewTask = useCallback(async () => {
    const bridge = window.craneDesktop;
    const id = composerID.trim();
    if (!bridge || !taskIDPattern.test(id) || !composerInput.trim()) return;
    setSubmitting(true);
    setComposerError("");
    const result = await bridge.submitTask(id, composerInput);
    setSubmitting(false);
    if (!result.ok) {
      setComposerError(result.error.message);
      return;
    }
    setComposerOpen(false);
    setComposerInput("");
    setSection("Executions");
    setSelectedId(result.data.id);
    setDetail(result.data);
    setDetailState("ready");
    setDetailMessage("");
    setActionMessage("");
    stopTaskEvents();
    await refresh("manual");
    startTaskEvents(result.data.id);
  }, [composerID, composerInput, refresh, startTaskEvents, stopTaskEvents]);

  const cancelSelectedTask = useCallback(async () => {
    const bridge = window.craneDesktop;
    if (!bridge || !selectedId) return;
    setCancellingId(selectedId);
    setActionMessage("");
    const result = await bridge.cancelTask(selectedId);
    setCancellingId(null);
    if (!result.ok) {
      setActionMessage(result.error.message);
      return;
    }
    setDetail(result.data);
    await refresh("manual");
  }, [refresh, selectedId]);

  useEffect(() => {
    if (!composerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) setComposerOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [composerOpen, submitting]);

  useEffect(() => () => {
    streamTokenRef.current += 1;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    const current = activeStreamRef.current;
    activeStreamRef.current = null;
    if (current) {
      try { current.unsubscribe(); } catch { /* cleanup is best-effort */ }
    }
  }, []);

  const counts = useMemo(() => ({
    active: tasks.filter((task) => !terminalStatuses.has(task.status)).length,
    completed: tasks.filter((task) => task.status === "succeeded").length,
    total: tasks.length,
  }), [tasks]);

  const banner = getBannerState(connection, health, connectionMessage, bridgeAvailable);

  return (
    <div className="app-shell" data-theme={theme}>
      <aside className={`sidebar ${sidebarOpen ? "" : "sidebar-collapsed"}`}>
        <div className="brand">
          <img className="brand-logo" src={theme === "dark" ? "./crane-mark-dark.webp" : "./crane-mark-light.webp"} alt="CRANE crane emblem" />
          {sidebarOpen && <div className="brand-copy"><strong>CRANE</strong><small>EXECUTION RUNTIME</small></div>}
          <button className="icon-button sidebar-toggle" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Toggle sidebar"><Menu size={17} /></button>
        </div>

        {sidebarOpen && <div className="workspace-switcher">
          <div className="workspace-avatar">L</div>
          <div className="workspace-copy"><strong>Local workspace</strong><small>On this device</small></div>
          <ChevronDown size={14} />
        </div>}

        <div className="nav-label">{sidebarOpen ? "WORKSPACE" : "NAV"}</div>
        <nav className="main-nav">
          {navItems.map(({ label, icon: Icon }) => (
            <button key={label} className={`nav-item ${section === label ? "active" : ""}`} onClick={() => setSection(label)} title={label}>
              <Icon size={17} strokeWidth={1.8} />
              {sidebarOpen && <span>{label}</span>}
              {sidebarOpen && label === "Executions" && counts.active > 0 && <span className="nav-count">{counts.active}</span>}
            </button>
          ))}
        </nav>

        {sidebarOpen && <div className="sidebar-bottom">
          <div className="sidebar-card">
            <div className="sidebar-card-icon"><HardDrive size={15} /></div>
            <div><strong>Local-first by design</strong><p>Task data stays in the supervisor-owned SQLite store.</p></div>
          </div>
          <button className="help-link" disabled><CircleHelp size={15} /> Documentation <ArrowUpRight size={12} /></button>
          <div className="profile-row"><div className="profile-avatar">A</div><div className="profile-copy"><strong>Personal environment</strong><small>Desktop client</small></div><MoreHorizontal size={16} /></div>
        </div>}
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>CRANE</span><span className="crumb-slash">/</span><strong>{section}</strong></div>
          <div className="topbar-actions">
            <div className="connection-pill"><span className={`status-dot ${connectionTone(connection)}`} /> {connectionLabels[connection]}</div>
            <button className="icon-button theme-toggle" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} title={theme === "dark" ? "Light theme" : "Dark theme"}>{theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}</button>
          </div>
        </header>

        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" /> LOCAL CONTROL PLANE</div>
              <h1>{section === "Overview" ? "Execution overview" : section}</h1>
              <p>{active.detail}. Data shown here comes from the authenticated local supervisor.</p>
            </div>
            <div className="heading-actions">
              <button className="button button-secondary" onClick={() => void refresh("manual")} disabled={busy}><RefreshCw size={15} className={busy ? "spin" : ""} /> {busy ? "Refreshing" : "Refresh"}</button>
              <button className="button button-primary" onClick={openComposer} disabled={!bridgeAvailable || connection !== "connected"} title={!bridgeAvailable ? "Desktop bridge unavailable" : connection !== "connected" ? "Connect the local supervisor first" : "Submit a bounded task through the authenticated supervisor"}>New execution</button>
            </div>
          </div>

          <div className={`runtime-banner ${banner.tone}`}>
            <div className="runtime-icon"><Activity size={18} /></div>
            <div className="runtime-message"><strong>{banner.title}</strong><span>{banner.body}</span></div>
            <div className="runtime-banner-right">
              <span className="runtime-state">{banner.state}</span>
              <span className="runtime-updated">{updatedAt ? `Updated ${formatTime(updatedAt.toISOString())}` : "Awaiting first probe"}</span>
            </div>
          </div>

          {section === "Overview" && (
            <Overview
              health={health}
              tasks={tasks}
              counts={counts}
              connection={connection}
              onNavigate={setSection}
              onSelect={(id) => { setSection("Executions"); void selectTask(id); }}
            />
          )}
          {section === "Executions" && (
            <Executions
              tasks={tasks}
              connection={connection}
              selectedId={selectedId}
              detail={detail}
              detailState={detailState}
              detailMessage={detailMessage}
              events={taskEvents}
              streamState={streamState}
              streamMessage={streamMessage}
              actionMessage={actionMessage}
              cancelling={cancellingId === selectedId && selectedId !== null}
              onSelect={(id) => void selectTask(id)}
              onRefresh={() => void refresh("manual")}
              onCancel={() => void cancelSelectedTask()}
              busy={busy}
            />
          )}
          {section === "MCP servers" && <EmptyState icon={Boxes} title="No MCP server data available" body="The authenticated /v1 API currently reports health and persisted tasks only. No MCP connections are inferred or displayed." footnote="Backend support unavailable" />}
          {section === "Approvals" && <EmptyState icon={ShieldCheck} title="No approval data available" body="The authenticated /v1 API does not expose approval records yet. No pending actions are fabricated here." footnote="Backend support unavailable" />}
          {section === "Settings" && <Settings appInfo={appInfo} bridgeAvailable={bridgeAvailable} />}
        </div>

        <footer className="statusbar">
          <div><span className={`status-dot ${connectionTone(connection)}`} /> <span>LOCAL MODE</span><span className="status-separator">/</span><span>{connection === "connected" ? "RUNTIME LINKED" : "RUNTIME OFFLINE"}</span></div>
          <div><LockKeyhole size={12} /> No cloud fallback <span className="status-separator">·</span> {appInfo ? `v${appInfo.version}` : "Desktop client"}</div>
        </footer>
      </main>

      {composerOpen && (
        <TaskComposer
          id={composerID}
          input={composerInput}
          error={composerError}
          submitting={submitting}
          onIDChange={setComposerID}
          onInputChange={setComposerInput}
          onSubmit={() => void submitNewTask()}
          onClose={() => setComposerOpen(false)}
        />
      )}
    </div>
  );
}

function getBannerState(
  connection: ConnectionState,
  health: CraneHealth | null,
  message: string,
  bridgeAvailable: boolean,
): { tone: string; title: string; body: string; state: string } {
  if (!bridgeAvailable) {
    return { tone: "problem", title: "Desktop bridge unavailable", body: "The restricted preload bridge did not initialize. No runtime data can be displayed.", state: "BRIDGE UNAVAILABLE" };
  }
  if (connection === "checking") {
    return { tone: "checking", title: "Connecting to local supervisor", body: "Reading authenticated health and persisted task data from 127.0.0.1.", state: "CONNECTING" };
  }
  if (connection === "recovering") {
    return { tone: "checking", title: "Reconnecting to local supervisor", body: message || "Retrying the authenticated local API.", state: "RECOVERING" };
  }
  if (connection === "disconnected") {
    return { tone: "problem", title: "CRANE runtime is not connected", body: message || "Start the Go supervisor, then refresh this view.", state: "NOT CONNECTED" };
  }
  if (connection === "error") {
    return { tone: "problem", title: "Supervisor data could not be loaded", body: message, state: "API ERROR" };
  }
  if (health && !health.ready) {
    return { tone: "problem", title: "Supervisor is reachable but not ready", body: `Runtime: ${health.runtime.detail}. Review component status below.`, state: "NOT READY" };
  }
  return { tone: "ready", title: "Supervisor connected and ready", body: `Protocol v${health?.version ?? "—"} · authenticated loopback only.`, state: "CONNECTED" };
}

function connectionTone(connection: ConnectionState): string {
  if (connection === "connected") return "online";
  if (connection === "error") return "error";
  if (connection === "checking" || connection === "recovering") return "warn";
  return "offline";
}

function Overview({
  health, tasks, counts, connection, onNavigate, onSelect,
}: {
  health: CraneHealth | null;
  tasks: CraneTask[];
  counts: { active: number; completed: number; total: number };
  connection: ConnectionState;
  onNavigate: (section: Section) => void;
  onSelect: (id: string) => void;
}) {
  return <>
    <section className="metrics-grid">
      <Metric icon={ListTodo} label="Active executions" value={connection === "connected" ? String(counts.active) : "—"} note="Non-terminal persisted tasks" />
      <Metric icon={Check} label="Completed tasks" value={connection === "connected" ? String(counts.completed) : "—"} note="Persisted succeeded tasks" />
      <Metric icon={HardDrive} label="Persisted tasks" value={connection === "connected" ? String(counts.total) : "—"} note="Read from SQLite by Go" />
      <Metric icon={Gauge} label="Overall readiness" value={health ? (health.ready ? "Ready" : "Not ready") : "—"} note="Supervisor, runtime and store" />
    </section>

    <section className="overview-grid">
      <div className="panel execution-panel">
        <div className="panel-heading"><div><h3>Recent executions</h3><p>Persisted tasks returned by the local supervisor</p></div><button className="subtle-link" onClick={() => onNavigate("Executions")}>View all <ArrowUpRight size={13} /></button></div>
        {connection !== "connected" ? (
          <DataState icon={Activity} title="Runtime data unavailable" body="Connect the local supervisor to load persisted executions." />
        ) : tasks.length === 0 ? (
          <DataState icon={ListTodo} title="No persisted tasks" body="Tasks created through the Go runtime will appear here with their real lifecycle status." />
        ) : (
          <div className="recent-list">
            {tasks.slice(0, 5).map((task) => (
              <button key={task.id} className="recent-row" onClick={() => onSelect(task.id)}>
                <div><strong>{task.id}</strong><span>Created {formatTime(task.created_at)}</span></div>
                <div><span className={`status-chip ${statusTone(task.status)}`}>{task.status}</span><ArrowUpRight size={13} /></div>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="right-stack">
        <div className="panel quick-panel">
          <div className="panel-heading"><div><h3>Runtime facts</h3><p>Reported by the Go supervisor</p></div><Workflow size={17} className="accent-icon" /></div>
          <div className="quick-action static"><span className="quick-icon blue"><LockKeyhole size={16} /></span><span><strong>Authenticated loopback</strong><small>127.0.0.1 · no renderer token access</small></span></div>
          <div className="quick-action static"><span className="quick-icon green"><HardDrive size={16} /></span><span><strong>SQLite-backed history</strong><small>Task data is read, never fabricated</small></span></div>
          <div className="quick-action static"><span className="quick-icon violet"><Bot size={16} /></span><span><strong>Bounded LangGraph execution</strong><small>One supervisor-owned Python child per task</small></span></div>
        </div>
        <div className="panel privacy-panel"><div className="privacy-icon"><LockKeyhole size={16} /></div><div><strong>Your machine. Your execution.</strong><p>Closing this desktop window does not stop supervisor-owned work.</p></div></div>
      </div>
    </section>

    <section className="panel system-panel">
      <div className="panel-heading"><div><h3>Runtime components</h3><p>Reported separately by the local supervisor</p></div><span className="panel-label">LOCAL ENVIRONMENT</span></div>
      <div className="component-grid">
        <ComponentStatus icon={Gauge} name="Go supervisor" detail={health ? (health.supervisor ? "Running" : "Unavailable") : "Not queried"} ok={health?.supervisor ?? null} />
        <ComponentStatus icon={Bot} name="Python agent" detail={health?.runtime.detail ?? "Not queried"} ok={health?.runtime.available ?? null} />
        <ComponentStatus icon={HardDrive} name="SQLite store" detail={health ? (health.persistence ? "Responding" : "Unavailable") : "Not queried"} ok={health?.persistence ?? null} />
        <ComponentStatus icon={Workflow} name="Overall readiness" detail={health ? (health.ready ? "All components ready" : "One or more components unavailable") : "Not queried"} ok={health?.ready ?? null} />
      </div>
    </section>
  </>;
}

function Executions({
  tasks, connection, selectedId, detail, detailState, detailMessage, events, streamState,
  streamMessage, actionMessage, cancelling, onSelect, onRefresh, onCancel, busy,
}: {
  tasks: CraneTask[];
  connection: ConnectionState;
  selectedId: string | null;
  detail: CraneTask | null;
  detailState: DetailState;
  detailMessage: string;
  events: TaskEventView[];
  streamState: StreamState;
  streamMessage: string;
  actionMessage: string;
  cancelling: boolean;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const canCancel = Boolean(detail) && !terminalStatuses.has(detail!.status) && detail!.status !== "cancelling";

  return <section className="executions-grid">
    <div className="panel task-panel">
      <div className="panel-heading">
        <div><h3>Persisted executions</h3><p>{connection === "connected" ? `${tasks.length} task${tasks.length === 1 ? "" : "s"} from SQLite` : "Awaiting supervisor connection"}</p></div>
        <button className="subtle-link" onClick={onRefresh} disabled={busy}><RefreshCw size={13} className={busy ? "spin" : ""} /> Refresh</button>
      </div>
      {connection !== "connected" ? (
        <DataState icon={Activity} title="Runtime data unavailable" body="Start the Go supervisor or refresh to retry the connection." />
      ) : tasks.length === 0 ? (
        <DataState icon={ListTodo} title="No persisted tasks" body="The task store is reachable and currently empty." />
      ) : (
        <div className="task-list">
          {tasks.map((task) => (
            <button key={task.id} className={`task-row ${selectedId === task.id ? "selected" : ""}`} onClick={() => onSelect(task.id)}>
              <div className="task-row-main"><strong>{task.id}</strong><span className={`status-chip ${statusTone(task.status)}`}>{task.status}</span></div>
              <div className="task-row-meta"><span>Created {formatTime(task.created_at)}</span><span>Updated {formatTime(task.updated_at)}</span></div>
              <div className="task-row-foot"><span>{task.has_checkpoint ? "Checkpoint persisted" : "No checkpoint"}</span><ArrowUpRight size={13} /></div>
            </button>
          ))}
        </div>
      )}
    </div>

    <div className="panel detail-panel">
      <div className="panel-heading">
        <div><h3>Execution details</h3><p>{selectedId ? selectedId : "Select a task to inspect its persisted state"}</p></div>
        {canCancel && <button className="button button-danger" onClick={onCancel} disabled={cancelling}><X size={14} /> {cancelling ? "Cancelling…" : "Cancel task"}</button>}
      </div>
      {actionMessage && <div className="action-message" role="alert">{actionMessage}</div>}
      {detailState === "idle" && <DataState icon={Workflow} title="No task selected" body="Choose a persisted execution from the list." />}
      {detailState === "loading" && <DataState icon={Activity} title="Loading task" body="Reading task details from the local supervisor." tone="loading" />}
      {detailState === "missing" && <DataState icon={ListTodo} title="Task not found" body={detailMessage || "This task is no longer present in the store."} tone="error" />}
      {detailState === "error" && <DataState icon={Activity} title="Task details unavailable" body={detailMessage} tone="error" />}
      {detailState === "ready" && detail && <>
        <TaskDetail task={detail} />
        <EventFeed events={events} state={streamState} message={streamMessage} />
      </>}
    </div>
  </section>;
}

function EventFeed({
  events, state, message,
}: {
  events: TaskEventView[];
  state: StreamState;
  message: string;
}) {
  return <div className="event-feed">
    <div className="event-feed-heading">
      <div><h4>Live execution events</h4><p>Persisted events streamed by the Go supervisor</p></div>
      <span className={`stream-chip ${state}`}>{streamLabel(state)}</span>
    </div>
    {message && <p className="stream-message">{message}</p>}
    {events.length === 0 ? (
      <div className="event-empty">No lifecycle events have been recorded for this task.</div>
    ) : (
      <div className="event-list">
        {events.map((event) => (
          <div className="event-row" key={event.id}>
            <span className="event-id">#{event.id}</span>
            <div className="event-copy"><strong>{event.type}</strong><span>{event.timestamp ? formatTime(event.timestamp) : "Timestamp not recorded"}</span></div>
            <code>{formatValue(event.payload)}</code>
          </div>
        ))}
      </div>
    )}
    {events.length === maxVisibleEvents && <div className="event-truncated">Showing the latest {maxVisibleEvents} events. Full history remains in SQLite.</div>}
  </div>;
}

function TaskComposer({
  id, input, error, submitting, onIDChange, onInputChange, onSubmit, onClose,
}: {
  id: string;
  input: string;
  error: string;
  submitting: boolean;
  onIDChange: (value: string) => void;
  onInputChange: (value: string) => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  const valid = taskIDPattern.test(id.trim()) && Boolean(input.trim());
  return <div className="modal-backdrop" role="presentation">
    <section className="composer-modal" role="dialog" aria-modal="true" aria-labelledby="composer-title">
      <div className="modal-heading">
        <div><h2 id="composer-title">New execution</h2><p>Admitted durably, then executed by the supervisor-owned Python runtime.</p></div>
        <button className="icon-button" onClick={onClose} disabled={submitting} aria-label="Close task composer"><X size={17} /></button>
      </div>
      <label className="composer-field">
        <span>TASK ID · 1–200 SAFE CHARACTERS</span>
        <input value={id} onChange={(event) => onIDChange(event.target.value)} maxLength={200} spellCheck={false} disabled={submitting} aria-label="Task ID" />
      </label>
      <label className="composer-field">
        <span>TASK INPUT · MAX 64 KIB</span>
        <textarea value={input} onChange={(event) => onInputChange(event.target.value)} placeholder="Describe the work for the local agent to perform…" maxLength={65536} disabled={submitting} aria-label="Task input" />
      </label>
      {error && <div className="composer-error" role="alert">{error}</div>}
      <div className="composer-context"><span><LockKeyhole size={12} /> Authenticated 127.0.0.1</span><span><Workflow size={12} /> No cloud execution</span></div>
      <div className="modal-footer">
        <span>Duplicate IDs are rejected before admission.</span>
        <div className="modal-actions">
          <button className="button button-secondary" onClick={onClose} disabled={submitting}>Cancel</button>
          <button className="button button-primary" onClick={onSubmit} disabled={!valid || submitting}>{submitting ? "Submitting…" : "Submit task"}</button>
        </div>
      </div>
    </section>
  </div>;
}

function TaskDetail({ task }: { task: CraneTask }) {
  return <div className="detail-body">
    <div className="detail-grid">
      <DetailItem label="Task ID" value={task.id} mono />
      <DetailItem label="Status" value={task.status} />
      <DetailItem label="Created" value={formatTime(task.created_at)} />
      <DetailItem label="Updated" value={formatTime(task.updated_at)} />
      <DetailItem label="Started" value={formatTime(task.started_at)} />
      <DetailItem label="Completed" value={formatTime(task.completed_at)} />
      <DetailItem label="Checkpoint" value={task.has_checkpoint ? "Persisted" : "None"} />
      <DetailItem label="Schema" value={task.schema_version || "Not recorded"} />
      <DetailItem label="Workflow reference" value={task.workflow_ref} />
      <DetailItem label="Error" value={task.error} />
    </div>
    <div className="detail-result"><span>Result</span><pre>{formatValue(task.result)}</pre></div>
    <div className="detail-note"><LockKeyhole size={13} /> Raw task input is intentionally omitted from the API response.</div>
  </div>;
}

function DetailItem({ label, value, mono = false }: { label: string; value?: string; mono?: boolean }) {
  return <div className="detail-item"><span>{label}</span><strong className={mono ? "mono" : ""}>{formatValue(value)}</strong></div>;
}

function Metric({ icon: Icon, label, value, note }: { icon: typeof Activity; label: string; value: string; note: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><Icon size={16} /></div><div className="metric-value">{value}</div><div className="metric-note">{note}</div></div>;
}

function ComponentStatus({ icon: Icon, name, detail, ok }: { icon: typeof Activity; name: string; detail: string; ok: boolean | null }) {
  const tone = ok === null ? "offline" : ok ? "online" : "error";
  return <div className="component-item"><div className="component-icon"><Icon size={16} /></div><div><strong>{name}</strong><small>{detail}</small></div><span className={`status-dot ${tone}`} /></div>;
}

function DataState({ icon: Icon, title, body, tone = "neutral" }: { icon: typeof Activity; title: string; body: string; tone?: "neutral" | "error" | "loading" }) {
  return <div className={`data-state ${tone}`}><div className="data-state-icon"><Icon size={20} /></div><strong>{title}</strong><p>{body}</p></div>;
}

function EmptyState({ icon: Icon, title, body, footnote }: { icon: typeof Activity; title: string; body: string; footnote: string }) {
  return <section className="panel full-empty"><div className="empty-icon"><Icon size={22} /></div><h2>{title}</h2><p>{body}</p><span className="empty-footnote"><Activity size={13} /> {footnote}</span></section>;
}

function Settings({ appInfo, bridgeAvailable }: { appInfo: CraneAppInfo | null; bridgeAvailable: boolean }) {
  return <div className="settings-grid">
    <section className="panel settings-panel"><div className="panel-heading"><div><h3>Desktop application</h3><p>Client information and local environment</p></div></div>
      <SettingRow label="Application" value={appInfo?.name ?? "CRANE Desktop"} /><SettingRow label="Version" value={appInfo?.version ?? "Development"} /><SettingRow label="Platform" value={appInfo?.platform ?? "Unknown"} /><SettingRow label="Runtime bridge" value={bridgeAvailable ? "Restricted preload IPC" : "Unavailable"} />
    </section>
    <section className="panel settings-panel"><div className="panel-heading"><div><h3>Privacy and execution</h3><p>Local-first defaults</p></div></div>
      <SettingRow label="Execution mode" value="Local only" /><SettingRow label="Cloud fallback" value="Disabled" /><SettingRow label="Telemetry" value="Off" /><SettingRow label="Runtime API" value="Authenticated 127.0.0.1:8787" /><SettingRow label="Installation token" value="Go/Electron owned; never exposed here" />
    </section>
  </div>;
}

function SettingRow({ label, value }: { label: string; value: string }) {
  return <div className="setting-row"><span>{label}</span><strong>{value}</strong></div>;
}

export default App;
