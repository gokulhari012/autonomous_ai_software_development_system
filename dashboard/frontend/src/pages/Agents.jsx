import React, { useState } from "react";
import { api, withProject } from "../api.js";
import { Badge, Card, ErrorBox, JsonView, Modal, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function Agents({ project }) {
  const { data, error } = usePoll(withProject("/api/agents", project), 3000, !!project);
  const [selected, setSelected] = useState(null);
  const history = usePoll(selected ? withProject(`/api/agents/${selected}/runs`, project) : null, 5000, !!selected);
  const [run, setRun] = useState(null);

  async function openRun(row) {
    setRun(await api(`/api/agent-runs/${row.id}`));
  }

  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  return (
    <div>
      <p className="hint">Status comes from recorded agent runs. An agent is shown ACTIVE only while its run is executing in a live controller.</p>
      <div className="cards">
        {data.map((agent) => (
          <div key={agent.name} className={`card clickable ${agent.active ? "active-card" : ""}`} onClick={() => setSelected(agent.name)}>
            <div className="card-title">{agent.name.replace(/_/g, " ")}</div>
            <div className="card-value"><Badge value={agent.status} /></div>
            <div className="card-sub">
              {agent.active ? <>task #{agent.current_task_id}: {agent.current_activity}</> : <>last activity {time(agent.last_activity)}</>}
            </div>
            <div className="card-sub">{agent.runs} run(s), {agent.errors} error(s){agent.duration_seconds !== null ? `, last ${agent.duration_seconds}s` : ""}</div>
          </div>
        ))}
      </div>
      {selected && (
        <Section title={`Execution history: ${selected}`} actions={<button onClick={() => setSelected(null)}>Close</button>}>
          <Table
            columns={[
              { key: "id", label: "Run" },
              { key: "task_id", label: "Task" },
              { key: "stage", label: "Stage" },
              { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "backend", label: "Backend" },
              { key: "started_at", label: "Started", render: (r) => time(r.started_at) },
              { key: "num_turns", label: "Turns" },
              { key: "output_summary", label: "Output", render: (r) => (r.output_summary || r.error_message || "").slice(0, 120) },
            ]}
            rows={history.data || []}
            onRowClick={openRun}
          />
        </Section>
      )}
      {run && (
        <Modal title={`Agent run #${run.run.id} (${run.run.agent_name})`} onClose={() => setRun(null)}>
          <div className="cards">
            <Card title="Status" value={<Badge value={run.run.status} />} />
            <Card title="Turns" value={run.run.num_turns ?? "-"} />
            <Card title="Tokens in/out" value={`${run.run.input_tokens ?? "-"} / ${run.run.output_tokens ?? "-"}`} />
            <Card title="Cost (SDK)" value={run.run.cost_usd != null ? `$${run.run.cost_usd.toFixed(4)}` : "-"} />
          </div>
          <h3>Input summary</h3>
          <pre className="json">{run.run.input_summary}</pre>
          <h3>Output summary</h3>
          <pre className="json">{run.run.output_summary || run.run.error_message}</pre>
          <JsonView value={run.run.output_json} />
          <h3>Recorded activity ({run.events.length})</h3>
          <div className="console small">
            {run.events.map((e) => <div key={e.id} className={`line ${e.event_type.includes("FAIL") || e.event_type.includes("DENIED") ? "err" : ""}`}>{time(e.ts)} {e.event_type} {e.summary}</div>)}
          </div>
        </Modal>
      )}
    </div>
  );
}
