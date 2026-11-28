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
  ["flask.lan_access", "Let other devices on my network (e.g. phone) open the staging app - anyone on the same network can reach it", "bool"],
  ["flask.staging_port", "Flask staging port", "int"],
  ["flask.qa_port", "Flask browser-test port", "int"],
  ["flask.health_path", "Flask health endpoint", "text"],
  ["pyside.startup_seconds", "Desktop app startup check (seconds)", "int"],
  ["project_notes", "Extra instructions for the Product Manager", "textarea"],
];
// Model and effort choices come from the backend, so these drop-downs are built from its "options".
const modelFields = (options) => {
  const models = options.models.map((m) => [m.id, m.label]);
  return [
    ["model", "Default Claude model", "select", models],
    ["fallback_model", "Fallback model", "select", [["", "No fallback"], ...models]],
    ["default_effort", "Default effort", "select", options.efforts.map((e) => [e, e])],
  ];
};
const WORKFLOW_FIELDS = [
  ["max_budget_usd", "Budget per run (USD, SDK-reported)", "float"], ["max_budget_usd_per_agent_call", "Budget per agent call (USD)", "float"],
  ["max_execution_minutes", "Maximum run time (minutes, usage-limit waits excluded)", "int"],
  ["agent_timeout_seconds", "Agent call timeout (seconds)", "int"], ["default_max_turns", "Default turns per agent call", "int"],
  ["max_agent_turns_per_task", "Maximum agent turns per task", "int"], ["transient_retry_attempts", "Retries for temporary API errors", "int"],
  ["max_stage_errors", "Agent errors per stage before blocking", "int"],
];

// A saved value that is not in the list (edited by hand in the JSON file) stays visible and selected.
function Select({ value, options, onChange, ...props }) {
  const current = value ?? "";
  const known = options.some(([v]) => v === current);
  return (
    <select {...props} value={current} onChange={(e) => onChange(e.target.value)}>
      {!known && <option value={current}>{current || "(not set)"}</option>}
      {options.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
    </select>
  );
}

const SPECIAL_NAMES = { qa: "QA", devops: "DevOps" };
const agentLabel = (name) => name.split("_").map((w) => SPECIAL_NAMES[w] || w[0].toUpperCase() + w.slice(1)).join(" ");

const TEAM_MODES = [
  ["full", "Full team - every agent runs"],
  ["custom", "Custom team - I choose the agents"],
  ["auto", "Automatic - sized to each task"],
];
const TEAM_HINTS = {
  full: "All 9 agents work on every task. The most thorough option, and the one that uses the most Claude usage.",
  custom: "Tick the agents this project needs. The Product Manager, Developer, Code Reviewer and Product Validator always run.",
  auto: "Each task gets the agents it needs, judged from its size and kind. Simple, documentation and test tasks skip the Architect and QA Engineer, " +
    "and skip the Security Auditor unless they touch security-sensitive code. Review and validation always run. " +
    "The team picked for a task is shown in the Console when the task starts.",
};

function AgentTeam({ roles, mode, agents, onChange }) {
  const chosen = agents || [];
  const optional = roles.filter((r) => !r.required).map((r) => r.name);
  function setMode(value) {
    onChange("agent_team.mode", value);
    if (value === "custom" && chosen.length === 0) onChange("agent_team.agents", optional);
  }
  function toggle(name, on) {
    onChange("agent_team.agents", optional.filter((a) => (a === name ? on : chosen.includes(a))));
  }
  return (
    <div>
      <div className="form-grid"><label>Agent team<Select value={mode} options={TEAM_MODES} onChange={setMode} /></label></div>
      <p className="hint">{TEAM_HINTS[mode]}</p>
      {mode === "custom" && (
        <div className="agent-list">
          {roles.map((r) => (
            <label key={r.name} className="agent-choice">
              <input type="checkbox" checked={r.required || chosen.includes(r.name)} disabled={r.required} onChange={(e) => toggle(r.name, e.target.checked)} />
              <span><strong>{agentLabel(r.name)}</strong><br /><span className="hint">{r.role}</span></span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function AgentModels({ options, values, onChange, disabled }) {
  const modelLabel = (id) => options.models.find((m) => m.id === id)?.label || id || "SDK default";
  const models = [["", `Default (${modelLabel(values.model)})`], ...options.models.map((m) => [m.id, m.label])];
  const efforts = [["", `Default (${values.default_effort})`], ...options.efforts.map((e) => [e, e])];
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Agent</th><th>Claude model</th><th>Effort level</th></tr></thead>
        <tbody>
          {options.agents.map((agent) => {
            const name = agentLabel(agent);
            return (
              <tr key={agent}>
                <th>{name}</th>
                <td><Select aria-label={`${name} model`} disabled={disabled} value={values[`agents.${agent}.model`]} options={models} onChange={(v) => onChange(`agents.${agent}.model`, v)} /></td>
                <td><Select aria-label={`${name} effort level`} disabled={disabled} value={values[`agents.${agent}.effort`]} options={efforts} onChange={(v) => onChange(`agents.${agent}.effort`, v)} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AutomaticModels({ options, values, onChange }) {
  const limits = options.upgrade_limits.map((m) => [m.id, m.label]);
  return (
    <>
      <div className="form-grid">
        <Field spec={["auto_model_selection", "Automatic model and effort selection (from each task's complexity)", "bool"]} value={values.auto_model_selection} onChange={onChange} />
        <Field spec={["auto_escalation", "Automatic upgrade on failure (retry with a stronger model and higher effort)", "bool"]} value={values.auto_escalation} onChange={onChange} />
        {values.auto_escalation && <Field spec={["escalation_max_model", "Highest model for upgrades", "select", limits]} value={values.escalation_max_model} onChange={onChange} />}
      </div>
      {values.auto_model_selection && (
        <p className="hint">Simple tasks use Claude Sonnet 5.5 at medium effort, standard tasks Claude Opus 5.5 at high, and complex tasks Claude Opus 5.5 at xhigh.
          The QA Engineer and DevOps Engineer run one effort level lower. While this is on, the per-agent choices below are kept but not used.</p>
      )}
      {values.auto_escalation && (
        <p className="hint">After each failed attempt (an agent error, invalid output, or work sent back by review, tests, scans or validation), that agent's next try
          goes one step up: higher effort first, then a stronger model, up to the highest model you choose, then max effort. Upgrades cost more per call; the budget limits still apply.</p>
      )}
    </>
  );
}

function TokenSaving({ project, values, saved, onChange }) {
  const [check, setCheck] = useState(null);
  const [busy, setBusy] = useState(false);
  async function runCheck() {
    setBusy(true); setCheck(null);
    try {
      const r = await api("/api/control/rtk_check", { method: "POST", body: { project } });
      setCheck({ ok: r.ok, output: r.output || JSON.stringify(r, null, 2) });
    } catch (err) { setCheck({ ok: false, output: err.message }); }
    finally { setBusy(false); }
  }
  const unsaved = values.rtk_path !== saved.rtk_path;
  return (
    <>
      <div className="form-grid">
        <Field spec={["rtk_enabled", "Use RTK (Rust Token Killer) to shorten agents' test output", "bool"]} value={values.rtk_enabled} onChange={onChange} />
        <label>RTK program path (optional)
          <input value={values.rtk_path ?? ""} placeholder="Empty = find rtk on PATH, e.g. C:\Tools\rtk\rtk.exe" onChange={(e) => onChange("rtk_path", e.target.value)} />
        </label>
      </div>
      <p className="hint">When on, the pytest commands agents run go through <code>rtk pytest</code>, which returns only the failures, so agents read fewer tokens.
        The permission rules still check the original command, and RTK can't add new commands. If RTK isn't installed or doesn't work, agents run the normal commands.
        Install on Windows: <code>winget install rtk-ai.rtk</code>. The controller's own test runs and reports are never shortened.</p>
      <div className="buttons">
        <button disabled={busy} onClick={runCheck}>{busy ? "Checking..." : "Check RTK"}</button>
        {unsaved && <span className="hint">Save first to check the new path.</span>}
      </div>
      {check && <div><h3>RTK: <span className={`badge ${check.ok ? "ok" : "bad"}`}>{check.ok ? "READY" : "NOT USABLE"}</span></h3><pre className="json">{check.output}</pre></div>}
    </>
  );
}

function Field({ spec, value, onChange }) {
  const [key, label, type, options] = spec;
  if (type === "bool") return <label className="check"><input type="checkbox" checked={!!value} onChange={(e) => onChange(key, e.target.checked)} /> {label}</label>;
  if (type === "select") return <label>{label}<Select value={value} options={options} onChange={(v) => onChange(key, v)} /></label>;
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
  const saveButton = <button onClick={save} disabled={!dirty}>Save {dirty ? `(${dirty} change${dirty > 1 ? "s" : ""})` : ""}</button>;
  const setWorkflow = (k, v) => setDraft((d) => ({ ...d, workflow: { ...d.workflow, [k]: v } }));
  const setProject = (k, v) => setDraft((d) => ({ ...d, project: { ...d.project, [k]: v } }));
  return (
    <div>
      {message && <div className="notice">{message}</div>}
      <ErrorBox error={error} />
      {selected && selected.controller.running && <div className="notice">The controller is running. Stop it before saving settings.</div>}
      <Section title={`Project settings: ${data.read_only.project_name}`} actions={saveButton}>
        <div className="form-grid">
          {PROJECT_FIELDS.map((spec) => <Field key={spec[0]} spec={spec} value={draft.project[spec[0]]} onChange={setProject} />)}
        </div>
      </Section>
      <Section title={`Agent team: ${data.read_only.project_name}`} actions={saveButton}>
        <AgentTeam roles={data.options.agent_roles} mode={draft.project["agent_team.mode"]} agents={draft.project["agent_team.agents"]} onChange={setProject} />
      </Section>
      <Section title="Global settings (all projects)">
        <div className="form-grid">
          {[...modelFields(data.options), ...WORKFLOW_FIELDS].map((spec) => <Field key={spec[0]} spec={spec} value={draft.workflow[spec[0]]} onChange={setWorkflow} />)}
        </div>
      </Section>
      <Section title="Agent models and effort (all projects)" actions={saveButton}>
        <AutomaticModels options={data.options} values={draft.workflow} onChange={setWorkflow} />
        {!draft.workflow.auto_model_selection && <p className="hint">Choose a model and effort level for each agent. "Default" follows the default model and default effort in Global settings.</p>}
        <AgentModels options={data.options} values={draft.workflow} onChange={setWorkflow} disabled={!!draft.workflow.auto_model_selection} />
      </Section>
      <Section title="Token saving (all projects)" actions={saveButton}>
        <TokenSaving project={project} values={draft.workflow} saved={data.workflow} onChange={setWorkflow} />
      </Section>
      <Section title="Fixed safety settings (not editable here)">
        <table><tbody>{Object.entries(data.read_only).map(([k, v]) => <tr key={k}><th>{k}</th><td>{String(v)}</td></tr>)}</tbody></table>
        <p className="hint">Production deployment stays disabled, review and validation stay mandatory, and main is never committed to or pushed.</p>
      </Section>
    </div>
  );
}
