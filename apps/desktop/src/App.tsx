import { useEffect, useState } from "react";
import {
  Activity, ArrowUpRight, Bot, Boxes, Check, ChevronDown, CircleHelp, Sun, Moon,
  Command, Cpu, Gauge, GitBranch, HardDrive, LayoutDashboard, ListTodo,
  LockKeyhole, Menu, MoreHorizontal, Plus, Search, Settings2, ShieldCheck,
  SlidersHorizontal, Sparkles, Terminal, Workflow, X,
} from "lucide-react";

type Section = "Overview" | "Executions" | "MCP servers" | "Approvals" | "Settings";
type AppInfo = { name: string; version: string; platform: string; runtimeConnected: boolean };

const navItems: { label: Section; icon: typeof LayoutDashboard; detail: string }[] = [
  { label: "Overview", icon: LayoutDashboard, detail: "Runtime pulse and recent activity" },
  { label: "Executions", icon: ListTodo, detail: "Task history and live execution" },
  { label: "MCP servers", icon: Boxes, detail: "Connected tools and transports" },
  { label: "Approvals", icon: ShieldCheck, detail: "Actions waiting for authorization" },
  { label: "Settings", icon: Settings2, detail: "Desktop and runtime preferences" },
];

function App() {
  const [section, setSection] = useState<Section>("Overview");
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [prompt, setPrompt] = useState("");
  const [showComposer, setShowComposer] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    window.craneDesktop?.getAppInfo().then(setAppInfo).catch(() => setAppInfo(null));
  }, []);

  const active = navItems.find((item) => item.label === section) ?? navItems[0];

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
              {sidebarOpen && label === "Approvals" && <span className="nav-count">0</span>}
            </button>
          ))}
        </nav>

        {sidebarOpen && <div className="sidebar-bottom">
          <div className="sidebar-card">
            <div className="sidebar-card-icon"><HardDrive size={15} /></div>
            <div><strong>Local-first by design</strong><p>Your task data stays on this device.</p></div>
          </div>
          <button className="help-link"><CircleHelp size={15} /> Documentation <ArrowUpRight size={12} /></button>
          <div className="profile-row"><div className="profile-avatar">A</div><div className="profile-copy"><strong>Personal environment</strong><small>Desktop client</small></div><MoreHorizontal size={16} /></div>
        </div>}
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>CRANE</span><span className="crumb-slash">/</span><strong>{section}</strong></div>
          <div className="topbar-actions">
            <div className="connection-pill"><span className="status-dot offline" /> Runtime disconnected</div>
            <button className="icon-button" aria-label="Search"><Search size={17} /></button>
            <button className="icon-button theme-toggle" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"} title={theme === "dark" ? "Light theme" : "Dark theme"}>{theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}</button>
            <div className="topbar-divider" />
            <button className="icon-button" aria-label="Command menu"><Command size={17} /></button>
          </div>
        </header>

        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" /> LOCAL CONTROL PLANE</div>
              <h1>{section === "Overview" ? "Execution overview" : section}</h1>
              <p>{active.detail}. Quiet by default. Full visibility when you need it.</p>
            </div>
            <div className="heading-actions">
              <button className="button button-secondary" onClick={() => setSection("Settings")}><SlidersHorizontal size={15} /> Preferences</button>
              <button className="button button-primary" onClick={() => setShowComposer(true)}><Plus size={16} /> New execution</button>
            </div>
          </div>

          <div className="runtime-banner">
            <div className="runtime-icon"><Activity size={18} /></div>
            <div className="runtime-message"><strong>CRANE runtime is not connected</strong><span>The desktop workspace is ready. Connect the local runtime API to submit tasks and load live execution data.</span></div>
            <div className="runtime-banner-right"><span className="runtime-state">NOT CONNECTED</span><button className="text-button" onClick={() => setSection("Settings")}>Connection settings <ArrowUpRight size={13} /></button></div>
          </div>

          {section === "Overview" && <Overview onNavigate={setSection} onNew={() => setShowComposer(true)} />}
          {section === "Executions" && <EmptyState icon={ListTodo} title="No executions yet" body="Tasks submitted to your local CRANE runtime will appear here, with their lifecycle, workflow steps and final results." />}
          {section === "MCP servers" && <EmptyState icon={Boxes} title="No MCP servers connected" body="Your configured stdio and Streamable HTTP servers will appear here after the runtime connection is available." />}
          {section === "Approvals" && <EmptyState icon={ShieldCheck} title="Nothing needs your approval" body="When a workflow reaches an action that requires authorization, the exact target and arguments will be shown here." />}
          {section === "Settings" && <Settings appInfo={appInfo} />}
        </div>

        <footer className="statusbar">
          <div><span className="status-dot offline" /> <span>LOCAL MODE</span><span className="status-separator">/</span><span>RUNTIME OFFLINE</span></div>
          <div><LockKeyhole size={12} /> No cloud fallback <span className="status-separator">·</span> {appInfo ? `v${appInfo.version}` : "Desktop preview"}</div>
        </footer>
      </main>

      {showComposer && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowComposer(false); }}>
        <section className="composer-modal">
          <div className="modal-heading"><div><div className="eyebrow">NEW TASK</div><h2>What should CRANE do?</h2></div><button className="icon-button" onClick={() => setShowComposer(false)} aria-label="Close"><X size={18} /></button></div>
          <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe a task for your local agent…" autoFocus />
          <div className="composer-context"><span><Cpu size={14} /> Local execution</span><span><GitBranch size={14} /> Workflow preview unavailable</span></div>
          <div className="modal-footer"><span>Runtime connection required before submission.</span><button className="button button-primary" disabled={!prompt.trim()} onClick={() => setShowComposer(false)}> <ArrowUpRight size={15} /> Close draft</button></div>
        </section>
      </div>}
    </div>
  );
}

function Overview({ onNavigate, onNew }: { onNavigate: (section: Section) => void; onNew: () => void }) {
  return <>
    <section className="metrics-grid">
      <Metric icon={ListTodo} label="Active executions" value="—" note="Waiting for runtime" />
      <Metric icon={Check} label="Completed today" value="—" note="No live data" />
      <Metric icon={Boxes} label="MCP servers" value="—" note="Not queried" />
      <Metric icon={ShieldCheck} label="Pending approvals" value="—" note="Not queried" />
    </section>

    <section className="overview-grid">
      <div className="panel execution-panel">
        <div className="panel-heading"><div><h3>Recent executions</h3><p>Your latest task activity</p></div><button className="subtle-link" onClick={() => onNavigate("Executions")}>View all <ArrowUpRight size={13} /></button></div>
        <div className="empty-activity">
          <div className="empty-orbit"><div className="orbit-core"><Workflow size={22} /></div><span className="orbit-dot dot-one" /><span className="orbit-dot dot-two" /><span className="orbit-dot dot-three" /></div>
          <strong>Nothing running in the background</strong>
          <p>Once connected, your executions will show up here. You can close this window without interrupting runtime-owned work.</p>
          <button className="button button-secondary" onClick={onNew}><Plus size={15} /> Draft a task</button>
        </div>
      </div>

      <div className="right-stack">
        <div className="panel quick-panel">
          <div className="panel-heading"><div><h3>Quick start</h3><p>Pick up where your work happens</p></div><Sparkles size={17} className="accent-icon" /></div>
          <button className="quick-action" onClick={onNew}><span className="quick-icon blue"><Terminal size={16} /></span><span><strong>Run a task</strong><small>Describe an outcome, not a script</small></span><ArrowUpRight size={14} /></button>
          <button className="quick-action" onClick={() => onNavigate("MCP servers")}><span className="quick-icon green"><Boxes size={16} /></span><span><strong>Manage MCP servers</strong><small>Connect tools to your runtime</small></span><ArrowUpRight size={14} /></button>
          <button className="quick-action" onClick={() => onNavigate("Approvals")}><span className="quick-icon violet"><ShieldCheck size={16} /></span><span><strong>Review approvals</strong><small>Authorize consequential actions</small></span><ArrowUpRight size={14} /></button>
        </div>
        <div className="panel privacy-panel"><div className="privacy-icon"><LockKeyhole size={16} /></div><div><strong>Your machine. Your execution.</strong><p>CRANE is local-first. Hosted providers and remote MCP servers are opt-in, never a silent fallback.</p></div></div>
      </div>
    </section>

    <section className="panel system-panel">
      <div className="panel-heading"><div><h3>Runtime components</h3><p>Connection state will be reported by the local supervisor</p></div><span className="panel-label">LOCAL ENVIRONMENT</span></div>
      <div className="component-grid">
        <ComponentStatus icon={Gauge} name="Go supervisor" state="Not connected" />
        <ComponentStatus icon={Bot} name="Python agent" state="Not connected" />
        <ComponentStatus icon={Workflow} name="Workflow engine" state="Not queried" />
        <ComponentStatus icon={HardDrive} name="SQLite store" state="Not queried" />
      </div>
    </section>
  </>;
}

function Metric({ icon: Icon, label, value, note }: { icon: typeof Activity; label: string; value: string; note: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><Icon size={16} /></div><div className="metric-value">{value}</div><div className="metric-note">{note}</div></div>;
}

function ComponentStatus({ icon: Icon, name, state }: { icon: typeof Activity; name: string; state: string }) {
  return <div className="component-item"><div className="component-icon"><Icon size={16} /></div><div><strong>{name}</strong><small>{state}</small></div><span className="status-dot offline" /></div>;
}

function EmptyState({ icon: Icon, title, body }: { icon: typeof Activity; title: string; body: string }) {
  return <section className="panel full-empty"><div className="empty-icon"><Icon size={22} /></div><h2>{title}</h2><p>{body}</p><span className="empty-footnote"><Activity size={13} /> Waiting for local runtime connection</span></section>;
}

function Settings({ appInfo }: { appInfo: AppInfo | null }) {
  return <div className="settings-grid">
    <section className="panel settings-panel"><div className="panel-heading"><div><h3>Desktop application</h3><p>Client information and local environment</p></div></div>
      <SettingRow label="Application" value="CRANE Desktop" /><SettingRow label="Version" value={appInfo?.version ?? "Development"} /><SettingRow label="Platform" value={appInfo?.platform ?? navigator.platform} /><SettingRow label="Runtime bridge" value="Not implemented yet" />
    </section>
    <section className="panel settings-panel"><div className="panel-heading"><div><h3>Privacy and execution</h3><p>Local-first defaults</p></div></div>
      <SettingRow label="Execution mode" value="Local only" /><SettingRow label="Cloud fallback" value="Disabled" /><SettingRow label="Telemetry" value="Off by default" /><SettingRow label="Runtime API" value="Awaiting Go local API" />
    </section>
  </div>;
}

function SettingRow({ label, value }: { label: string; value: string }) {
  return <div className="setting-row"><span>{label}</span><strong>{value}</strong></div>;
}

export default App;