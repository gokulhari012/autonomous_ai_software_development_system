import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, ErrorBox, Section, Table } from "../components.jsx";

const BRIEF_HINT = `Describe the application in your own words, for example:

Purpose: a calculator web app for everyday use.
Users: anyone with a browser; no login.
Features: + - x / with operator precedence, decimals, clear, keyboard input, history of the last 20 calculations.
Technology: Flask web app with SQLite (or: PySide6 desktop app).
Data to store: calculation history (expression, result, time).
UI: one page, responsive, light/dark theme.
Deployment: local only.`;

const EMPTY = {
  name: "", path: "", mode: "new", brief: "", application_types: ["flask"],
  stop_after: "requirements", auto_resume_on_limit: true, git_push_enabled: false, git_remote_url: "",
};

export default function Projects({ project, projects, reloadProjects, selectProject }) {
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [brief, setBrief] = useState(null);
  const [briefText, setBriefText] = useState("");
  const selected = projects.find((p) => p.id === project);

  useEffect(() => {
    setBrief(null);
    if (!selected || selected.mode !== "new") return;
    api(`/api/projects/${selected.id}/brief`).then((b) => { setBrief(b); setBriefText(b.is_template ? "" : b.brief); }).catch(() => setBrief(null));
  }, [selected && selected.id]);

  function set(key, value) { setForm({ ...form, [key]: value }); }
  function toggleType(type) {
    const types = form.application_types.includes(type) ? form.application_types.filter((t) => t !== type) : [...form.application_types, type];
    set("application_types", types.length ? types : [type]);
  }

  async function create(andStart) {
    setBusy(true); setError(null); setMessage(null);
    try {
      const settings = {
        application_types: form.application_types, stop_after: form.stop_after,
        auto_resume_on_limit: form.auto_resume_on_limit, git_push_enabled: form.git_push_enabled,
      };
      if (form.git_push_enabled) settings.git_remote_url = form.git_remote_url.trim();
      const result = await api("/api/control/create_project", {
        method: "POST", body: { name: form.name, path: form.path, mode: form.mode, brief: form.brief, settings },
      });
      reloadProjects();
      if (result.project_id) selectProject(result.project_id);
      if (andStart && result.project_id) {
        await api("/api/control/start", { method: "POST", body: { project: result.project_id } });
        setMessage(`Project created and started. Follow it on the Overview and Live Console pages.`);
      } else {
        setMessage(`Project created. Select it in the top bar and press Start in the Control Panel when you are ready.`);
      }
      setForm(EMPTY);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function saveBrief() {
    setError(null);
    try {
      const b = await api(`/api/projects/${selected.id}/brief`, { method: "PUT", body: { brief: briefText } });
      setBrief(b);
      setMessage("Project description saved.");
    } catch (err) { setError(err); }
  }

  async function remove(p) {
    const typed = window.prompt(`Delete project "${p.name}"?\n\nOnly its settings file (config/projects/${p.id}.json) is removed. ` +
      `The project folder, its git history and the recorded reports are NOT deleted.\n\nType the project id to confirm: ${p.id}`);
    if (typed !== p.id) return;
    setError(null);
    try {
      await api("/api/control/delete_project", { method: "POST", body: { project: p.id } });
      setMessage(`Project ${p.name} deleted (folder kept).`);
      reloadProjects();
    } catch (err) { setError(err); }
  }

  const isNew = form.mode === "new";
  return (
    <div>
      {message && <div className="notice">{message}</div>}
      <ErrorBox error={error} />
      <Section title="Create a project">
        <div className="form-grid">
          <label>Project name<input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Calculator App" /></label>
          <label>Project folder (absolute path)<input value={form.path} onChange={(e) => set("path", e.target.value)} placeholder="C:\Users\you\Documents\projects\calculator-app" /></label>
          <fieldset>
            <legend>Type</legend>
            <label><input type="radio" checked={isNew} onChange={() => set("mode", "new")} /> New project (build from scratch; the folder may be empty or not exist yet)</label>
            <label><input type="radio" checked={!isNew} onChange={() => set("mode", "existing")} /> Existing project (a git repository you already have)</label>
          </fieldset>
          <fieldset>
            <legend>Application type</legend>
            <label><input type="checkbox" checked={form.application_types.includes("flask")} onChange={() => toggleType("flask")} /> Flask web app</label>
            <label><input type="checkbox" checked={form.application_types.includes("pyside")} onChange={() => toggleType("pyside")} /> PySide desktop app</label>
          </fieldset>
        </div>
        <label className="block">{isNew ? "Project description (saved as requirements/brief.md)" : "What should be built or changed (given to the Product Manager)"}
          <textarea className="big" rows={18} value={form.brief} onChange={(e) => set("brief", e.target.value)} placeholder={BRIEF_HINT} />
        </label>
        <div className="form-grid">
          <label>When should a run finish?
            <select value={form.stop_after} onChange={(e) => set("stop_after", e.target.value)}>
              <option value="task">After each completed task (you start the next one)</option>
              <option value="requirements">When all approved requirements are done</option>
              <option value="manual">Never - keep running until I press Stop</option>
            </select>
          </label>
          <label className="check"><input type="checkbox" checked={form.auto_resume_on_limit} onChange={(e) => set("auto_resume_on_limit", e.target.checked)} /> Resume automatically when the Claude usage limit resets</label>
          <label className="check"><input type="checkbox" checked={form.git_push_enabled} onChange={(e) => set("git_push_enabled", e.target.checked)} /> Push commits to GitHub (branch autonomous-development only, never main, never forced)</label>
          {form.git_push_enabled && (
            <label>GitHub repository URL (leave empty if the repository already has an 'origin' remote)
              <input value={form.git_remote_url} onChange={(e) => set("git_remote_url", e.target.value)} placeholder="https://github.com/you/calculator-app.git" />
            </label>
          )}
        </div>
        <div className="buttons">
          <button disabled={busy || !form.name || !form.path} onClick={() => create(false)}>Create project</button>
          <button disabled={busy || !form.name || !form.path || (isNew && !form.brief.trim())} onClick={() => create(true)}>Create and start</button>
          {busy && <span className="hint">working...</span>}
        </div>
        <p className="hint">The folder must be inside the dashboard's allowed project roots. GitHub push uses the git login already on this computer (Git Credential Manager or an SSH key); no token is stored here.</p>
      </Section>

      {selected && selected.mode === "new" && brief && (
        <Section title={`Project description: ${selected.name}`}>
          <p className="hint">{brief.path}</p>
          <textarea className="big" rows={16} value={briefText} onChange={(e) => setBriefText(e.target.value)} placeholder={BRIEF_HINT} />
          <div className="buttons"><button onClick={saveBrief} disabled={!briefText.trim()}>Save description</button></div>
          {brief.open_questions_file && <><h3>Open questions from the Product Manager</h3><pre className="json">{brief.open_questions_file}</pre>
            <p className="hint">Answer them in the Control Panel (Questions), then resume.</p></>}
        </Section>
      )}

      <Section title="Projects">
        <Table
          columns={[
            { key: "name", label: "Name" }, { key: "mode", label: "Mode" },
            { key: "status", label: "Status", render: (p) => <Badge value={p.status} /> },
            { key: "controller", label: "Controller", render: (p) => <Badge value={p.controller.state} /> },
            { key: "path", label: "Folder" },
            { key: "actions", label: "Actions", render: (p) => (
              <span>
                <button onClick={() => selectProject(p.id)}>{p.id === project ? "Selected" : "Select"}</button>
                <button className="danger" onClick={() => remove(p)} disabled={p.controller.running}>Delete</button>
              </span>
            ) },
          ]}
          rows={projects}
          empty="No projects yet - create one above."
        />
      </Section>
    </div>
  );
}
