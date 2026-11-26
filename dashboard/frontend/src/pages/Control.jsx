import React, { useState } from "react";
import { api, artifactUrl, withProject } from "../api.js";
import { Badge, ErrorBox, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function Control({ project, projects, reloadProjects }) {
  const approvals = usePoll(withProject("/api/approvals", project), 5000, !!project);
  const reports = usePoll(withProject("/api/reports", project), 10000, !!project);
  const [output, setOutput] = useState(null);
  const [error, setError] = useState(null);
  const [form, setForm] = useState({ name: "", path: "", mode: "existing" });
  const [bot, setBot] = useState(false);
  const selected = projects.find((p) => p.id === project);

  async function control(action, body = {}, confirmText = null) {
    if (confirmText && !window.confirm(confirmText)) return;
    setError(null);
    try {
      const result = await api(`/api/control/${action}`, { method: "POST", body: { project, ...body } });
      setOutput(result.output || JSON.stringify(result, null, 2));
      approvals.reload();
      reloadProjects();
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div>
      <Section title="Execution">
        <p>Controller state: <Badge value={selected ? selected.controller.state : "IDLE"} /></p>
        <div className="buttons">
          <button onClick={() => control("start", { with_telegram_bot: bot }, "Start autonomous execution?")}>Start execution</button>
          <label><input type="checkbox" checked={bot} onChange={(e) => setBot(e.target.checked)} /> with Telegram command bot</label>
          <button onClick={() => control("pause")}>Pause</button>
          <button onClick={() => control("resume")}>Resume</button>
          <button className="danger" onClick={() => control("stop", {}, "Stop safely after the current operation?")}>Stop safely</button>
          <button onClick={() => control("generate_report")}>Generate final report</button>
        </div>
        <p className="hint">Pause finishes the current safe operation first. Stop prevents new tasks and shuts down gracefully.</p>
      </Section>
      <ErrorBox error={error} />
      {output && <pre className="json">{output}</pre>}
      <Section title="Pending approvals (human decisions)">
        <Table
          columns={[
            { key: "id", label: "#" }, { key: "kind", label: "Kind" }, { key: "subject", label: "Subject" },
            { key: "status", label: "Status", render: (a) => <Badge value={a.status} /> },
            { key: "details", label: "Details", render: (a) => JSON.stringify(a.details || {}).slice(0, 160) },
            { key: "requested_at", label: "Requested", render: (a) => time(a.requested_at) },
            {
              key: "actions", label: "Decision", render: (a) => a.status !== "PENDING" ? "-" : (
                a.kind === "REQUIREMENT" ? (
                  <span>
                    <button onClick={() => control("approve_requirement", { code: a.subject }, `Approve requirement ${a.subject}?`)}>Approve</button>
                    <button onClick={() => control("reject_requirement", { code: a.subject }, `Reject requirement ${a.subject}?`)}>Reject</button>
                  </span>
                ) : (
                  <span>
                    <button onClick={() => control("approve", { approval_id: a.id }, `Approve ${a.kind} #${a.id}?`)}>Approve</button>
                    <button onClick={() => control("reject", { approval_id: a.id, note: window.prompt("Reason (optional)") || "" })}>Reject</button>
                  </span>
                )
              ),
            },
          ]}
          rows={approvals.data || []}
          empty="No approvals."
        />
      </Section>
      <Section title="Projects">
        <form className="inline-form" onSubmit={(e) => { e.preventDefault(); control(form.mode === "new" ? "new_project" : "open_project", form); }}>
          <input placeholder="Project name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} aria-label="Project name" />
          <input placeholder="Absolute project path" value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} aria-label="Project path" size={50} />
          <select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })} aria-label="Mode">
            <option value="existing">Open existing project</option>
            <option value="new">Start a new project</option>
          </select>
          <button type="submit">Add project</button>
        </form>
        <p className="hint">Paths must be inside the allowed project roots configured for the dashboard. Select the project in the top bar, then start execution.</p>
        <Table columns={[
          { key: "name", label: "Name" }, { key: "mode", label: "Mode" }, { key: "status", label: "Status" },
          { key: "path", label: "Path" }, { key: "controller", label: "Controller", render: (p) => <Badge value={p.controller.state} /> },
        ]} rows={projects} />
      </Section>
      <Section title="Reports">
        <Table columns={[
          { key: "name", label: "Report", render: (r) => <a href={artifactUrl(r.path)} target="_blank" rel="noreferrer">{r.name}</a> },
          { key: "modified", label: "Modified", render: (r) => new Date(r.modified * 1000).toLocaleString() },
        ]} rows={reports.data || []} empty="No reports yet." />
      </Section>
    </div>
  );
}
