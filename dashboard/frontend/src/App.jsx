import React, { useEffect, useState } from "react";
import { api, withProject } from "./api.js";
import { Badge } from "./components.jsx";
import { useEventStream, usePoll } from "./hooks.js";
import Login from "./pages/Login.jsx";
import Overview from "./pages/Overview.jsx";
import Agents from "./pages/Agents.jsx";
import Console from "./pages/Console.jsx";
import Tasks from "./pages/Tasks.jsx";
import Testing from "./pages/Testing.jsx";
import Security from "./pages/Security.jsx";
import GitView from "./pages/GitView.jsx";
import Deployments from "./pages/Deployments.jsx";
import Usage from "./pages/Usage.jsx";
import Control from "./pages/Control.jsx";

const PAGES = [
  ["overview", "Overview", Overview],
  ["agents", "Agent Monitor", Agents],
  ["console", "Live Console", Console],
  ["tasks", "Task Manager", Tasks],
  ["testing", "Testing Center", Testing],
  ["security", "Security Center", Security],
  ["git", "Git Management", GitView],
  ["deployments", "Deployment Monitor", Deployments],
  ["usage", "Usage & Cost", Usage],
  ["control", "Control Panel", Control],
];

function currentPage() {
  const hash = window.location.hash.replace("#", "");
  return PAGES.some(([key]) => key === hash) ? hash : "overview";
}

export default function App() {
  const [user, setUser] = useState(undefined);
  const [page, setPage] = useState(currentPage());
  const [project, setProject] = useState(localStorage.getItem("autodev.project") || "");

  useEffect(() => {
    api("/api/auth/me").then(setUser).catch(() => setUser(null));
    const onHash = () => setPage(currentPage());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (user === undefined) return <div className="center">Loading...</div>;
  if (user === null) return <Login onLogin={setUser} />;
  return <Shell user={user} page={page} project={project} setProject={(p) => { localStorage.setItem("autodev.project", p); setProject(p); }} />;
}

function Shell({ user, page, project, setProject }) {
  const projects = usePoll("/api/projects", 10000);
  const effectiveProject = project || (projects.data && projects.data[0] ? projects.data[0].id : "");
  const stream = useEventStream(effectiveProject);
  const selected = (projects.data || []).find((p) => p.id === effectiveProject);
  const Page = PAGES.find(([key]) => key === page)[2];

  async function logout() {
    await api("/api/auth/logout", { method: "POST" }).catch(() => null);
    window.location.reload();
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">Autonomous Dev</div>
        <nav>
          {PAGES.map(([key, label]) => (
            <a key={key} href={`#${key}`} className={key === page ? "active" : ""}>{label}</a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>Signed in as {user.username}</div>
          <button className="link" onClick={logout}>Log out</button>
        </div>
      </aside>
      <main className="content">
        <header className="topbar">
          <label>
            Project{" "}
            <select value={effectiveProject} onChange={(e) => setProject(e.target.value)} aria-label="Select project">
              {(projects.data || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          {selected && <span>Controller <Badge value={selected.controller.state} /></span>}
          <span>Event stream <Badge value={stream.status} /></span>
        </header>
        {projects.data && projects.data.length === 0 && (
          <div className="notice">No project yet. Start the controller once (python main.py run) or add a project in the Control Panel.</div>
        )}
        <Page project={effectiveProject} events={stream.events} projects={projects.data || []} reloadProjects={projects.reload} />
      </main>
    </div>
  );
}

export { withProject };
