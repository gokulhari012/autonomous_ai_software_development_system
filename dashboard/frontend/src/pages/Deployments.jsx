import React from "react";
import { artifactUrl, withProject } from "../api.js";
import { Badge, Card, ErrorBox, JsonView, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function Deployments({ project }) {
  const { data, error } = usePoll(withProject("/api/deployments", project), 4000, !!project);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  const latest = data.deployments[0];
  return (
    <div>
      <p className="hint">Local staging only. Production deployment is disabled in the controller.</p>
      <div className="cards">
        <Card title="Staging status" value={<Badge value={data.active.length ? data.active[0].status : (latest ? latest.status : "NOT_APPLICABLE")} />} sub={data.active.length ? data.active[0].url : "no staging environment running"} />
        <Card title="Application health" value={latest ? (latest.health_status ? "reported" : "-") : "-"} sub={latest && latest.health_status ? latest.health_status.slice(0, 120) : ""} />
        <Card title="Method" value={latest ? latest.method : "-"} sub={latest && latest.detail && latest.detail.containers ? "docker compose" : "local process / desktop launch"} />
        <Card title="Last validation" value={<Badge value={data.validations[0] ? data.validations[0].status : "NOT_APPLICABLE"} />} sub={data.validations[0] ? (data.validations[0].output_summary || "").slice(0, 100) : ""} />
      </div>
      <Section title="Deployment history">
        <Table columns={[
          { key: "id", label: "#" }, { key: "task_id", label: "Task" }, { key: "environment", label: "Env" }, { key: "method", label: "Method" },
          { key: "status", label: "Status", render: (d) => <Badge value={d.status} /> }, { key: "url", label: "URL" },
          { key: "started_at", label: "Started", render: (d) => time(d.started_at) }, { key: "stopped_at", label: "Stopped", render: (d) => time(d.stopped_at) },
          { key: "log_path", label: "Startup log", render: (d) => (d.log_path ? <a href={artifactUrl(d.log_path)} target="_blank" rel="noreferrer">log</a> : "-") },
          { key: "detail", label: "Detail", render: (d) => <JsonView value={d.detail} /> },
        ]} rows={data.deployments} />
      </Section>
    </div>
  );
}
