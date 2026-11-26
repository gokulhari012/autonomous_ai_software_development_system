import React, { useState } from "react";
import { api, artifactUrl, withProject } from "../api.js";
import { Badge, ErrorBox, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

function QuestionSet({ item, onAnswer }) {
  const [text, setText] = useState("");
  const questions = (item.details && item.details.questions) || [];
  const pending = item.status === "PENDING";
  return (
    <div className="question-set">
      <div><Badge value={item.status} /> Question set #{item.id} ({(item.details && item.details.stage) || ""}) asked {time(item.requested_at)}</div>
      <ol>{questions.map((q, i) => <li key={i}>{q}</li>)}</ol>
      {pending ? (
        <>
          <textarea className="big" rows={8} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Answer every question here, e.g. '1. Web app (Flask). 2. Store the last 20 calculations...'" aria-label={`Answers for question set ${item.id}`} />
          <div className="buttons">
            <button disabled={!text.trim()} onClick={() => onAnswer(item.id, text, false)}>Save answers</button>
            <button disabled={!text.trim()} onClick={() => onAnswer(item.id, text, true)}>Save answers and resume</button>
          </div>
        </>
      ) : (
        item.decision_note && <><div className="hint">Your answer:</div><pre className="json">{item.decision_note}</pre></>
      )}
    </div>
  );
}

export default function Control({ project, projects, reloadProjects }) {
  const approvals = usePoll(withProject("/api/approvals", project), 5000, !!project);
  const questions = usePoll(withProject("/api/questions", project), 5000, !!project);
  const overview = usePoll(withProject("/api/overview", project), 5000, !!project);
  const reports = usePoll(withProject("/api/reports", project), 10000, !!project);
  const [output, setOutput] = useState(null);
  const [error, setError] = useState(null);
  const [bot, setBot] = useState(true);
  const [maxTasks, setMaxTasks] = useState("");
  const selected = projects.find((p) => p.id === project);

  async function control(action, body = {}, confirmText = null) {
    if (confirmText && !window.confirm(confirmText)) return;
    setError(null);
    try {
      const result = await api(`/api/control/${action}`, { method: "POST", body: { project, ...body } });
      setOutput(result.output || JSON.stringify(result, null, 2));
      approvals.reload(); questions.reload(); overview.reload(); reloadProjects();
    } catch (err) {
      setError(err);
    }
  }

  const startOptions = { with_telegram_bot: bot, ...(maxTasks ? { max_tasks: Number(maxTasks) } : {}) };
  const otherApprovals = (approvals.data || []).filter((a) => a.kind !== "QUESTIONS");
  const openQuestions = (questions.data || []).filter((q) => q.status === "PENDING");
  const wait = overview.data && overview.data.limit_wait;

  return (
    <div>
      <Section title="Execution">
        <p>Controller state: <Badge value={selected ? selected.controller.state : "IDLE"} /> {selected && selected.status === "AWAITING_INPUT" && <Badge value="AWAITING_INPUT" />}</p>
        {wait && wait.until && (
          <div className="notice">Waiting for the Claude usage limit to reset (until {new Date(wait.until * 1000).toLocaleString()}). It resumes automatically.
            <button onClick={() => control("resume_now")}>Resume now</button></div>
        )}
        <div className="buttons">
          <button onClick={() => control("start", startOptions, "Start autonomous execution?")} disabled={selected && selected.controller.running}>Start</button>
          <label>Max tasks this run <input type="number" min="1" value={maxTasks} onChange={(e) => setMaxTasks(e.target.value)} placeholder="config" style={{ width: 90 }} /></label>
          <label><input type="checkbox" checked={bot} onChange={(e) => setBot(e.target.checked)} /> with Telegram command bot</label>
        </div>
        <div className="buttons">
          <button onClick={() => control("pause")}>Pause</button>
          <button onClick={() => control("resume")}>Resume</button>
          <button className="danger" onClick={() => control("stop", {}, "Stop safely after the current operation?")}>Stop safely</button>
          <button onClick={() => control("generate_report")}>Generate final report</button>
        </div>
        <p className="hint">Pause finishes the current stage first. Stop prevents new work and shuts down gracefully. Finish point, GitHub push and auto-resume are set in Settings.</p>
      </Section>
      <ErrorBox error={error} />
      {output && <pre className="json">{output}</pre>}

      <Section title={`Questions from the Product Manager${openQuestions.length ? ` (${openQuestions.length} waiting)` : ""}`}>
        {(questions.data || []).length === 0 && <p className="empty">No questions so far.</p>}
        {(questions.data || []).map((item) => (
          <QuestionSet key={item.id} item={item} onAnswer={(id, text, resume) => control("answer_questions", { approval_id: id, note: text, resume, ...startOptions })} />
        ))}
      </Section>

      <Section title="Approvals (human decisions)">
        <Table
          columns={[
            { key: "id", label: "#" }, { key: "kind", label: "Kind" }, { key: "subject", label: "Subject" },
            { key: "status", label: "Status", render: (a) => <Badge value={a.status} /> },
            { key: "details", label: "Details", render: (a) => JSON.stringify(a.details || {}).slice(0, 200) },
            { key: "requested_at", label: "Requested", render: (a) => time(a.requested_at) },
            {
              key: "actions", label: "Decision", render: (a) => a.status !== "PENDING" ? (a.decision_note || "-") : (
                a.kind === "REQUIREMENT" ? (
                  <span>
                    <button onClick={() => control("approve_requirement", { code: a.subject }, `Approve requirement ${a.subject}?`)}>Approve</button>
                    <button onClick={() => control("reject_requirement", { code: a.subject }, `Reject requirement ${a.subject}?`)}>Reject</button>
                  </span>
                ) : (
                  <span>
                    <button onClick={() => control("approve", { approval_id: a.id }, `Approve ${a.kind} #${a.id}?`)}>Approve</button>
                    <button onClick={() => { const note = window.prompt("Reason (optional)"); if (note !== null) control("reject", { approval_id: a.id, note }); }}>Reject</button>
                  </span>
                )
              ),
            },
          ]}
          rows={otherApprovals}
          empty="No approvals."
        />
      </Section>

      <Section title="Reports">
        <Table columns={[
          { key: "name", label: "Report", render: (r) => <a href={artifactUrl(r.path)} target="_blank" rel="noreferrer">{r.name}</a> },
          { key: "modified", label: "Modified", render: (r) => new Date(r.modified * 1000).toLocaleString() },
        ]} rows={reports.data || []} empty="No reports yet." />
      </Section>
      <p className="hint">Create, edit and delete projects on the Projects page. Retry or cancel tasks in the Task Manager.</p>
    </div>
  );
}
