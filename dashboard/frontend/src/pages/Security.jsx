import React, { useState } from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { withProject } from "../api.js";
import { Badge, Card, ErrorBox, JsonView, Modal, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"];

export default function Security({ project }) {
  const { data, error } = usePoll(withProject("/api/security", project), 6000, !!project);
  const [tool, setTool] = useState("");
  const [status, setStatus] = useState("");
  const [detail, setDetail] = useState(null);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  const findings = data.findings.filter((f) => (!tool || f.tool_name === tool) && (!status || f.status === status));
  const chart = SEVERITIES.map((s) => ({ severity: s, open: data.by_severity[s] || 0 }));
  return (
    <div>
      <div className="cards">
        {["bandit", "semgrep", "pip_audit"].map((t) => {
          const last = data.scans.find((s) => s.tool_name === t);
          return <Card key={t} title={t} value={<Badge value={last ? last.status : "NOT_APPLICABLE"} />} sub={last ? `${last.findings_count} finding(s), ${time(last.created_at)}${last.error ? " - " + last.error.slice(0, 80) : ""}` : "never ran"} />;
        })}
        <Card title="Security Auditor (agent)" value={data.findings.filter((f) => f.auditor_assessment).length}
          sub={`finding(s) assessed with evidence; ${data.findings.filter((f) => f.tool_name === "security_auditor").length} manual finding(s)`} />
      </div>
      <Section title="Open findings by severity">
        {Object.keys(data.by_severity).length === 0 && <p className="hint">No open findings.</p>}
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={chart}><XAxis dataKey="severity" /><YAxis allowDecimals={false} /><Tooltip /><Bar dataKey="open" fill="#c62828" isAnimationActive={false} /></BarChart>
        </ResponsiveContainer>
      </Section>
      <Section title="Findings" actions={
        <span className="filters">
          <select value={tool} onChange={(e) => setTool(e.target.value)} aria-label="Tool"><option value="">all tools</option>{Object.keys(data.by_tool).map((t) => <option key={t}>{t}</option>)}</select>
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status"><option value="">all statuses</option>{["OPEN", "VERIFIED", "FALSE_POSITIVE_SUSPECTED", "NEEDS_REVIEW", "FIXED", "ACCEPTED_EXCEPTION"].map((s) => <option key={s}>{s}</option>)}</select>
        </span>
      }>
        <Table
          columns={[
            { key: "id", label: "#" }, { key: "tool_name", label: "Tool" },
            { key: "severity", label: "Severity", render: (f) => <Badge value={f.severity} /> },
            { key: "status", label: "Fix status", render: (f) => <Badge value={f.status} /> },
            { key: "file_path", label: "Location", render: (f) => `${f.file_path || "-"}:${f.line_number || ""}` },
            { key: "rule_id", label: "Rule" },
            { key: "description", label: "Details", render: (f) => (f.description || "").slice(0, 120) },
            { key: "baseline", label: "Pre-existing", render: (f) => (f.baseline ? "yes" : "no") },
          ]}
          rows={findings}
          onRowClick={setDetail}
        />
      </Section>
      <Section title="Scan history">
        <Table columns={[
          { key: "id", label: "#" }, { key: "task_id", label: "Task" }, { key: "tool_name", label: "Tool" },
          { key: "status", label: "Status", render: (s) => <Badge value={s.status} /> },
          { key: "findings_count", label: "Findings" }, { key: "error", label: "Error" },
          { key: "created_at", label: "When", render: (s) => time(s.created_at) },
        ]} rows={data.scans} />
      </Section>
      {detail && (
        <Modal title={`Finding #${detail.id}`} onClose={() => setDetail(null)}>
          <p><Badge value={detail.severity} /> <Badge value={detail.status} /> {detail.tool_name} {detail.rule_id}</p>
          <p>{detail.file_path}:{detail.line_number}</p>
          <pre className="json">{detail.description}</pre>
          <h3>Auditor assessment</h3>
          <JsonView value={detail.auditor_assessment} />
        </Modal>
      )}
    </div>
  );
}
