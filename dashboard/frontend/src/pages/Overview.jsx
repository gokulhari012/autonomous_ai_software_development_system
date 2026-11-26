import React from "react";
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { withProject } from "../api.js";
import { Badge, Card, ErrorBox, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

const COLORS = { COMPLETED: "#2e7d32", IN_PROGRESS: "#1565c0", PENDING: "#90a4ae", BLOCKED: "#ef6c00", ON_HOLD: "#f9a825", CANCELLED: "#616161", FAILED: "#c62828" };

export default function Overview({ project, events }) {
  const { data, error } = usePoll(withProject("/api/overview", project), 4000, !!project);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  const taskStatuses = Object.entries(data.tasks).filter(([k]) => k !== "total").map(([name, value]) => ({ name, value }));
  const recent = (events.length ? events.slice(-15).reverse() : data.recent_events);
  return (
    <div>
      <div className="cards">
        <Card title="Project" value={data.project.name} sub={`${data.project.mode || "?"} mode | branch ${data.project.dev_branch || "-"}`} />
        <Card title="Controller" value={<Badge value={data.controller.state} />} sub={data.last_run ? `last run ${time(data.last_run.started_at)} (${data.last_run.status})` : "never run"} />
        <Card title="Workflow stage" value={data.current_task ? data.current_task.stage : data.project.current_stage || "-"} sub={data.current_task ? `task #${data.current_task.id} ${data.current_task.title}` : "no task in progress"} />
        <Card title="Progress" value={`${data.progress.completed} / ${data.progress.total}`} sub={data.progress.basis} />
        <Card title="Active agents" value={data.active_agents.length} sub={data.active_agents.map((a) => a.agent_name).join(", ") || "none executing"} />
        <Card title="Completed tasks" value={data.tasks.COMPLETED || 0} sub={`of ${data.tasks.total}`} />
        <Card title="Failed tasks" value={data.tasks.FAILED || 0} />
        <Card title="Blocked tasks" value={data.tasks.BLOCKED || 0} sub={data.tasks.ON_HOLD ? `${data.tasks.ON_HOLD} awaiting approval` : ""} />
      </div>
      {data.attention.map((item, i) => (
        <div key={i} className={`attention ${item.level === "error" ? "bad" : item.level === "action" ? "warn" : "info"}`}>
          <strong>{item.title}</strong>{item.detail ? <div className="hint">{item.detail.slice(0, 300)}</div> : null}
          <div>{item.fix} <a href={item.level === "action" ? "#control" : "#troubleshoot"}>Open</a></div>
        </div>
      ))}
      {data.last_push && (
        <p className="hint">Last GitHub push: {data.last_push.ok ? "OK" : "FAILED"} ({data.last_push.context}, {data.last_push.at})</p>
      )}
      <div className="grid-2">
        <Section title="Task status">
          {taskStatuses.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie data={taskStatuses} dataKey="value" nameKey="name" outerRadius={90} label isAnimationActive={false}>
                  {taskStatuses.map((s) => <Cell key={s.name} fill={COLORS[s.name] || "#78909c"} />)}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          ) : <p className="empty">No tasks yet.</p>}
        </Section>
        <Section title="Recent activity">
          <Table
            columns={[
              { key: "ts", label: "Time", render: (e) => time(e.ts) },
              { key: "event_type", label: "Event" },
              { key: "agent_name", label: "Agent" },
              { key: "summary", label: "Summary", render: (e) => <span title={e.summary}>{(e.summary || "").slice(0, 120)}</span> },
            ]}
            rows={recent}
          />
        </Section>
      </div>
    </div>
  );
}
