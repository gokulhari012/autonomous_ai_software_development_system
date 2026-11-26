import React from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { withProject } from "../api.js";
import { Card, ErrorBox, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function Usage({ project }) {
  const { data, error } = usePoll(withProject("/api/usage", project), 10000, !!project);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  const agents = Object.entries(data.per_agent).map(([name, v]) => ({ name, ...v }));
  const t = data.totals;
  return (
    <div>
      <p className="hint">{data.note}</p>
      <div className="cards">
        <Card title="Agent runs" value={t.agent_runs} sub={t.scripted_runs ? `${t.scripted_runs} scripted (test backend)` : ""} />
        <Card title="Input tokens" value={t.input_tokens.toLocaleString()} />
        <Card title="Output tokens" value={t.output_tokens.toLocaleString()} />
        <Card title="Estimated API cost" value={`$${t.cost_usd.toFixed(2)}`} />
        <Card title="Task repair attempts" value={t.task_repair_attempts} />
        <Card title="Total execution time" value={`${Math.round(t.execution_seconds / 60)} min`} />
      </div>
      <div className="grid-2">
        <Section title="Tokens by agent">
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={agents}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="name" tick={{ fontSize: 10 }} /><YAxis /><Tooltip /><Legend />
              <Bar dataKey="input_tokens" fill="#1565c0" /><Bar dataKey="output_tokens" fill="#6a1b9a" /></BarChart>
          </ResponsiveContainer>
        </Section>
        <Section title="Estimated cost over time">
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={data.cost_timeline}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="hour" tick={{ fontSize: 10 }} /><YAxis /><Tooltip />
              <Line type="monotone" dataKey="cost_usd" stroke="#c62828" /></LineChart>
          </ResponsiveContainer>
        </Section>
      </div>
      <Section title="Per agent">
        <Table columns={[
          { key: "name", label: "Agent" }, { key: "runs", label: "Runs" }, { key: "turns", label: "Turns" }, { key: "errors", label: "Errors" },
          { key: "duration_seconds", label: "Duration (s)", render: (a) => Math.round(a.duration_seconds) },
          { key: "cost_usd", label: "Cost ($)", render: (a) => a.cost_usd.toFixed(4) },
        ]} rows={agents} />
      </Section>
      <Section title="Controller runs">
        <Table columns={[
          { key: "id", label: "#" }, { key: "status", label: "Status" }, { key: "started_at", label: "Started", render: (r) => time(r.started_at) },
          { key: "finished_at", label: "Finished", render: (r) => time(r.finished_at) }, { key: "stop_reason", label: "Reason" },
          { key: "tasks_completed", label: "Completed" }, { key: "tasks_blocked", label: "Blocked" },
        ]} rows={data.runs} />
      </Section>
    </div>
  );
}
