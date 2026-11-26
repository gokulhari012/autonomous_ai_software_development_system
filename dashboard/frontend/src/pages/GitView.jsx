import React from "react";
import { withProject } from "../api.js";
import { Card, ErrorBox, Section, Table, time } from "../components.jsx";
import { usePoll } from "../hooks.js";

export default function GitView({ project }) {
  const { data, error } = usePoll(withProject("/api/git", project), 8000, !!project);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <p>Loading...</p>;
  if (!data.available) return <p className="empty">{data.reason}</p>;
  const latest = data.checkpoints[0];
  return (
    <div>
      <p className="hint">Read-only view. Destructive git actions (reset, force push, branch deletion) are never available from the dashboard.</p>
      <div className="cards">
        <Card title="Current branch" value={data.branch} sub={`base: ${data.base_branch || "-"}`} />
        <Card title="HEAD" value={(data.head || "").slice(0, 10)} />
        <Card title="Modified files" value={data.modified_files.length} />
        <Card title="Latest checkpoint" value={latest ? latest.git_commit.slice(0, 10) : "-"} sub={latest ? `task #${latest.task_id} ${time(latest.created_at)}` : "none yet"} />
      </div>
      <Section title="Git status">
        <Table columns={[{ key: "code", label: "Status" }, { key: "path", label: "File" }]} rows={data.modified_files} empty="Working tree clean." />
      </Section>
      <Section title="Task to commit mapping (checkpoints)">
        <Table columns={[
          { key: "task_id", label: "Task" }, { key: "task_title", label: "Title" },
          { key: "git_commit", label: "Commit", render: (c) => c.git_commit.slice(0, 12) },
          { key: "branch", label: "Branch" }, { key: "created_at", label: "When", render: (c) => time(c.created_at) },
        ]} rows={data.checkpoints} />
      </Section>
      <Section title="Commit history">
        <Table columns={[
          { key: "sha", label: "Commit", render: (c) => c.sha.slice(0, 10) }, { key: "subject", label: "Message" },
          { key: "author", label: "Author" }, { key: "task_id", label: "Task" }, { key: "date", label: "Date", render: (c) => time(c.date) },
        ]} rows={data.commits} />
      </Section>
    </div>
  );
}
