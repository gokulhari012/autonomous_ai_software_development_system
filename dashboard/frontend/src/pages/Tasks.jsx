import React, { useState } from "react";
import { api, withProject } from "../api.js";
import { Badge, ErrorBox, Modal, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function Tasks({ project }) {
  const { data, error, reload } = usePoll(withProject("/api/tasks", project), 4000, !!project);
  const reqs = usePoll(withProject("/api/requirements", project), 10000, !!project);
  const [detail, setDetail] = useState(null);
  const [message, setMessage] = useState(null);

  async function open(row) {
    setDetail(await api(`/api/tasks/${row.id}`));
  }
  async function act(action, taskId) {
    if (!window.confirm(`${action.replace("_", " ")} task #${taskId}?`)) return;
    try {
      const result = await api(`/api/control/${action}`, { method: "POST", body: { project, task_id: taskId } });
      setMessage(result.output || "done");
      reload();
    } catch (err) {
      setMessage(err.message);
    }
  }

  if (error) return <ErrorBox error={error} />;
  return (
    <div>
      {message && <div className="notice">{message}</div>}
      <Section title="Tasks">
        <Table
          columns={[
            { key: "id", label: "#" },
            { key: "title", label: "Title" },
            { key: "priority", label: "Priority" },
            { key: "status", label: "Status", render: (t) => <Badge value={t.status} /> },
            { key: "stage", label: "Stage" },
            { key: "assigned_agent", label: "Agent" },
            { key: "retry_count", label: "Retries", render: (t) => `${t.retry_count}/${t.max_retries}` },
            { key: "depends_on", label: "Depends on", render: (t) => (t.depends_on.length ? t.depends_on.map((d) => `#${d}`).join(", ") : "-") },
            { key: "requirement_code", label: "Req" },
            {
              key: "actions", label: "Actions", render: (t) => (
                <span onClick={(e) => e.stopPropagation()}>
                  {t.status === "BLOCKED" && <button onClick={() => act("retry_task", t.id)}>Retry</button>}
                  {!["COMPLETED", "CANCELLED"].includes(t.status) && <button onClick={() => act("cancel_task", t.id)}>Cancel</button>}
                </span>
              ),
            },
          ]}
          rows={data || []}
          onRowClick={open}
        />
      </Section>
      <Section title="Requirements registry">
        <Table
          columns={[
            { key: "code", label: "Code" },
            { key: "title", label: "Title" },
            { key: "priority", label: "P" },
            { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "source", label: "Source" },
            { key: "acceptance_criteria", label: "Acceptance criteria", render: (r) => (r.acceptance_criteria || []).length },
            { key: "validation_evidence", label: "Evidence", render: (r) => (r.validation_evidence ? "yes" : "-") },
          ]}
          rows={reqs.data || []}
        />
      </Section>
      {detail && (
        <Modal title={`Task #${detail.task.id}: ${detail.task.title}`} onClose={() => setDetail(null)}>
          <p><Badge value={detail.task.status} /> stage {detail.task.stage} | retries {detail.task.retry_count}/{detail.task.max_retries}</p>
          {detail.task.blocked_reason && <div className="error-box">{detail.task.blocked_reason}</div>}
          <h3>Acceptance criteria</h3>
          <ol>{(detail.task.acceptance_criteria || []).map((c, i) => <li key={i}>{c}</li>)}</ol>
          <h3>History</h3>
          <Table columns={[
            { key: "created_at", label: "When", render: (h) => time(h.created_at) },
            { key: "from_stage", label: "From" }, { key: "to_stage", label: "To" },
            { key: "to_status", label: "Status", render: (h) => <Badge value={h.to_status} /> },
            { key: "reason", label: "Reason" },
          ]} rows={detail.history} />
          <h3>Issues</h3>
          <Table columns={[
            { key: "id", label: "#" }, { key: "source_agent", label: "Source" },
            { key: "severity", label: "Severity", render: (i) => <Badge value={i.severity} /> },
            { key: "status", label: "Status", render: (i) => <Badge value={i.status} /> },
            { key: "title", label: "Title" }, { key: "file_path", label: "File" },
          ]} rows={detail.issues} />
          <h3>Agent runs</h3>
          <Table columns={[
            { key: "id", label: "#" }, { key: "agent_name", label: "Agent" }, { key: "stage", label: "Stage" },
            { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "output_summary", label: "Summary", render: (r) => (r.output_summary || r.error_message || "").slice(0, 100) },
          ]} rows={detail.agent_runs} />
          <h3>Test runs</h3>
          <Table columns={[
            { key: "id", label: "#" }, { key: "stage", label: "Stage" }, { key: "test_type", label: "Layer" },
            { key: "status", label: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "passed", label: "Passed" }, { key: "failed", label: "Failed" },
          ]} rows={detail.test_runs} />
        </Modal>
      )}
    </div>
  );
}
