import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Section } from "../components.jsx";

const PROJECT_FIELDS = [
  ["stop_after", "When a run finishes", "select", [["task", "After each completed task"], ["requirements", "When all approved requirements are done"], ["manual", "Never - until I press Stop"]]],
  ["auto_resume_on_limit", "Resume automatically after the Claude usage limit resets", "bool"],
  ["git_push_enabled", "Push commits to GitHub (development branch only)", "bool"],
  ["git_remote", "Git remote name", "text"],
  ["git_remote_url", "GitHub repository URL (used to add the remote if it is missing)", "text"],
  ["application_types", "Application types (comma separated: flask, pyside, python)", "list"],
  ["max_tasks_per_run", "Maximum tasks per run", "int"],
  ["max_retries_per_task", "Repair attempts per task before it is blocked", "int"],
  ["max_parallel_tasks", "Tasks in parallel (1 is safest)", "int"],
  ["auto_approve_inferred_requirements", "Auto-approve requirements the agents infer (otherwise you approve them)", "bool"],
  ["flask.staging_port", "Flask staging port", "int"],
  ["flask.qa_port", "Flask browser-test port", "int"],
  ["flask.health_path", "Flask health endpoint", "text"],
  ["pyside.startup_seconds", "Desktop app startup check (seconds)", "int"],
  ["project_notes", "Extra instructions for the Product Manager", "textarea"],
];
const WORKFLOW_FIELDS = [
  ["model", "Claude model", "text"], ["fallback_model", "Fallback model", "text"],
  ["default_effort", "Default effort", "select", [["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"], ["max", "max"]]],
  ["max_budget_usd", "Budget per run (USD, SDK-reported)", "float"], ["max_budget_usd_per_agent_call", "Budget per agent call (USD)", "float"],
  ["max_execution_minutes", "Maximum run time (minutes, usage-limit waits excluded)", "int"],
  ["agent_timeout_seconds", "Agent call timeout (seconds)", "int"], ["default_max_turns", "Default turns per agent call", "int"],
  ["max_agent_turns_per_task", "Maximum agent turns per task", "int"], ["transient_retry_attempts", "Retries for temporary API errors", "int"],
  ["max_stage_errors", "Agent errors per stage before blocking", "int"],
];

function Field({ spec, value, onChange }) {
  const [key, label, type, options] = spec;
  if (type === "bool") return <label className="check"><input type="checkbox" checked={!!value} onChange={(e) => onChange(key, e.target.checked)} /> {label}</label>;
  if (type === "select") return <label>{label}<select value={value ?? ""} onChange={(e) => onChange(key, e.target.value)}>{options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}</select></label>;
  if (type === "textarea") return <label className="block">{label}<textarea className="big" rows={8} value={value ?? ""} onChange={(e) => onChange(key, e.target.value)} /></label>;
  if (type === "list") return <label>{label}<input value={(value || []).join(", ")} onChange={(e) => onChange(key, e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} /></label>;
  const numeric = type === "int" || type === "float";
  return (
    <label>{label}
      <input type={numeric ? "number" : "text"} step={type === "float" ? "0.1" : "1"} value={value ?? ""}
        onChange={(e) => onChange(key, numeric ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)} />
    </label>
  );
}

export default function Settings({ project, projects }) {
  const [data, setData] = useState(null);
  const [draft, setDraft] = useState({ project: {}, workflow: {} });
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const selected = projects.find((p) => p.id === project);

  async function load() {
    if (!project) return;
    try {
      const d = await api(`/api/projects/${project}/settings`);
      setData(d);
      setDraft({ project: { ...d.project }, workflow: { ...d.workflow } });
      setError(null);
    } catch (err) { setError(err); }
  }
  useEffect(() => { load(); }, [project]);

  function changed(section) {
    return Object.fromEntries(Object.entries(draft[section]).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(data[section][k])));
  }

  async function save() {
    setError(null); setMessage(null);
    try {
      const d = await api(`/api/projects/${project}/settings`, { method: "PUT", body: { project: changed("project"), workflow: changed("workflow") } });
      setData(d);
      setDraft({ project: { ...d.project }, workflow: { ...d.workflow } });
      setMessage("Saved and validated by the controller. Changes apply from the next Start.");
    } catch (err) { setError(err); }
  }

  if (!project) return <p className="empty">Select a project first.</p>;
  if (!data) return <ErrorBox error={error} />;
  const dirty = Object.keys(changed("project")).length + Object.keys(changed("workflow")).length;
  return (
    <div>
      {message && <div className="notice">{message}</div>}
      <ErrorBox error={error} />
      {selected && selected.controller.running && <div className="notice">The controller is running. Stop it before saving settings.</div>}
      <Section title={`Project settings: ${data.read_only.project_name}`} actions={<button onClick={save} disabled={!dirty}>Save {dirty ? `(${dirty} change${dirty > 1 ? "s" : ""})` : ""}</button>}>
        <div className="form-grid">
          {PROJECT_FIELDS.map((spec) => <Field key={spec[0]} spec={spec} value={draft.project[spec[0]]} onChange={(k, v) => setDraft({ ...draft, project: { ...draft.project, [k]: v } })} />)}
        </div>
      </Section>
      <Section title="Global settings (all projects)">
        <div className="form-grid">
          {WORKFLOW_FIELDS.map((spec) => <Field key={spec[0]} spec={spec} value={draft.workflow[spec[0]]} onChange={(k, v) => setDraft({ ...draft, workflow: { ...draft.workflow, [k]: v } })} />)}
        </div>
      </Section>
      <Section title="Fixed safety settings (not editable here)">
        <table><tbody>{Object.entries(data.read_only).map(([k, v]) => <tr key={k}><th>{k}</th><td>{String(v)}</td></tr>)}</tbody></table>
        <p className="hint">Production deployment stays disabled, review and validation stay mandatory, and main is never committed to or pushed.</p>
      </Section>
    </div>
  );
}
