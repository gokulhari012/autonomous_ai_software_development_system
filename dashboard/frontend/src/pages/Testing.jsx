import React, { useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, artifactUrl, withProject } from "../api.js";
import { Badge, Card, ErrorBox, Modal, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

const LAYERS = [
  ["pytest", "pytest (unit / API / database / integration)"],
  ["e2e", "Playwright browser tests"],
  ["qt", "pytest-qt desktop tests"],
  ["gui", "PyAutoGUI desktop automation"],
  ["acceptance", "Acceptance tests"],
];

export default function Testing({ project }) {
  const { data, error } = usePoll(withProject("/api/tests", project), 5000, !!project);
  const [detail, setDetail] = useState(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  const chart = [...data.runs].reverse().slice(-30).map((r) => ({ name: `#${r.id} ${r.test_type}`, passed: r.passed, failed: r.failed + r.errors }));

  return (
    <div>
      <div className="cards">
        {LAYERS.map(([key, label]) => {
          const run = data.latest_by_layer[key];
          return <Card key={key} title={label} value={<Badge value={run ? run.status : "NOT_APPLICABLE"} />} sub={run ? `${run.passed}/${run.total} passed | ${run.summary}` : "no runs for this layer"} />;
        })}
      </div>
      <Section title="Passed / failed per run">
        {chart.length ? (
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={chart}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="name" hide />
              <YAxis allowDecimals={false} />
              <Tooltip />
              <Legend />
              <Bar dataKey="passed" stackId="a" fill="#2e7d32" isAnimationActive={false} />
              <Bar dataKey="failed" stackId="a" fill="#c62828" isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        ) : <p className="empty">No test runs yet.</p>}
      </Section>
      <Section title="Test runs">
        <Table
          columns={[
            { key: "id", label: "#" }, { key: "task_id", label: "Task" }, { key: "stage", label: "Stage" },
            { key: "test_type", label: "Layer" }, { key: "tool_name", label: "Tool" },
            { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "total", label: "Total" }, { key: "passed", label: "Passed" },
            { key: "failed", label: "Failed", render: (r) => r.failed + r.errors },
            { key: "created_at", label: "When", render: (r) => time(r.created_at) },
          ]}
          rows={data.runs}
          onRowClick={async (r) => setDetail(await api(`/api/tests/${r.id}`))}
        />
      </Section>
      {detail && (
        <Modal title={`Test run #${detail.run.id} (${detail.run.test_type})`} onClose={() => setDetail(null)}>
          <p><Badge value={detail.run.status} /> {detail.run.summary}</p>
          <pre className="json">{detail.run.command}</pre>
          {detail.coverage && <p>Coverage: {detail.coverage.percent_covered_display || Math.round(detail.coverage.percent_covered)}%</p>}
          {detail.run.junit_path && <p><a href={artifactUrl(detail.run.junit_path)} target="_blank" rel="noreferrer">JUnit XML</a> | <a href={artifactUrl(detail.run.report_path)} target="_blank" rel="noreferrer">JSON report</a></p>}
          <h3>Test cases</h3>
          <Table columns={[
            { key: "nodeid", label: "Test" },
            { key: "status", label: "Status", render: (c) => <Badge value={c.status} /> },
            { key: "duration_seconds", label: "Seconds", render: (c) => (c.duration_seconds || 0).toFixed(2) },
            { key: "message", label: "Message", render: (c) => <span title={c.message}>{(c.message || "").slice(0, 160)}</span> },
          ]} rows={detail.cases} />
          <h3>Artifacts (screenshots, traces, logs)</h3>
          <div className="artifacts">
            {detail.artifacts.map((path) => (
              /\.(png|jpg)$/i.test(path)
                ? <a key={path} href={artifactUrl(path)} target="_blank" rel="noreferrer"><img src={artifactUrl(path)} alt={path} /></a>
                : <div key={path}><a href={artifactUrl(path)} target="_blank" rel="noreferrer">{path.split(/[\\/]/).slice(-2).join("/")}</a></div>
            ))}
            {!detail.artifacts.length && <p className="empty">No artifacts.</p>}
          </div>
        </Modal>
      )}
    </div>
  );
}
