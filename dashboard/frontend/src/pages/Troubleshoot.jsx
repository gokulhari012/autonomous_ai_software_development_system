import React, { useState } from "react";
import { api, withProject } from "../api.js";
import { ErrorBox, Section } from "../components.jsx";
import { usePoll } from "../hooks.js";

const LEVEL_CLASS = { error: "bad", action: "warn", info: "info" };

export default function Troubleshoot({ project }) {
  const overview = usePoll(withProject("/api/overview", project), 5000, !!project);
  const [output, setOutput] = useState({});
  const [busy, setBusy] = useState("");
  const [filter, setFilter] = useState("");
  const [logLines, setLogLines] = useState(300);
  const logs = usePoll(withProject(`/api/logs?lines=${logLines}${filter ? `&contains=${encodeURIComponent(filter)}` : ""}`, project), 5000, !!project);

  async function run(action, label) {
    setBusy(action);
    try {
      const result = await api(`/api/control/${action}`, { method: "POST", body: { project } });
      setOutput({ ...output, [action]: { ok: result.ok, text: result.output || JSON.stringify(result, null, 2), label } });
    } catch (err) {
      setOutput({ ...output, [action]: { ok: false, text: err.message, label } });
    } finally { setBusy(""); }
  }

  const items = (overview.data && overview.data.attention) || [];
  return (
    <div>
      <Section title="Needs attention">
        {items.length === 0 && <p className="empty">Nothing needs your attention.</p>}
        {items.map((item, i) => (
          <div key={i} className={`attention ${LEVEL_CLASS[item.level] || ""}`}>
            <strong>{item.title}</strong>
            {item.detail && <div className="hint">{item.detail}</div>}
            <div>How to fix: {item.fix}</div>
          </div>
        ))}
      </Section>
      <Section title="Checks">
        <div className="buttons">
          <button disabled={!!busy} onClick={() => run("doctor", "Environment check")}>Check environment (doctor)</button>
          <button disabled={!!busy} onClick={() => run("validate_config", "Configuration")}>Validate configuration</button>
          <button disabled={!!busy} onClick={() => run("git_check", "GitHub connection")}>Test GitHub connection</button>
          <button disabled={!!busy} onClick={() => run("telegram_test", "Telegram")}>Send Telegram test message</button>
          {busy && <span className="hint">running {busy}...</span>}
        </div>
        {Object.entries(output).map(([key, o]) => (
          <div key={key}><h3>{o.label}: <span className={`badge ${o.ok ? "ok" : "bad"}`}>{o.ok ? "OK" : "PROBLEM"}</span></h3><pre className="json">{o.text}</pre></div>
        ))}
        <p className="hint">If a task is blocked because a Python package is missing, install it into the controller's environment from a terminal
          (<code>.venv\Scripts\python.exe -m pip install &lt;package&gt;</code>) or ask for a simpler stack in the project settings, then press Retry in the Task Manager.
          Package installation is deliberately not available from the browser.</p>
      </Section>
      <Section title="Logs" actions={
        <span className="filters">
          <input placeholder="filter text (e.g. ERROR)" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter logs" />
          <select value={logLines} onChange={(e) => setLogLines(Number(e.target.value))} aria-label="Lines">
            {[100, 300, 1000, 3000].map((n) => <option key={n} value={n}>{n} lines</option>)}
          </select>
        </span>
      }>
        <ErrorBox error={logs.error} />
        {logs.data && Object.entries(logs.data).map(([label, lines]) => (
          <div key={label}>
            <h3>{label}</h3>
            <div className="console small">{lines.length ? lines.map((l, i) => <div key={i} className={`line ${/ERROR|WARNING|Traceback/.test(l) ? "err" : ""}`}>{l}</div>) : <div className="line">(empty)</div>}</div>
          </div>
        ))}
      </Section>
    </div>
  );
}
