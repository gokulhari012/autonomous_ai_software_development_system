import React, { useEffect, useMemo, useRef, useState } from "react";
import { api, withProject } from "../api.js";
import { time } from "../components.jsx";

const AGENTS = ["", "product_manager", "architect", "developer", "code_reviewer", "qa_engineer", "security_auditor", "bug_fixer", "devops_engineer", "product_validator"];

export default function Console({ project, events }) {
  const [agent, setAgent] = useState("");
  const [taskId, setTaskId] = useState("");
  const [type, setType] = useState("");
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [older, setOlder] = useState([]);
  const bottom = useRef(null);

  const all = useMemo(() => {
    const seen = new Set();
    return [...older, ...events].filter((e) => (seen.has(e.id) ? false : seen.add(e.id)));
  }, [older, events]);
  const types = useMemo(() => Array.from(new Set(all.map((e) => e.event_type))).sort(), [all]);
  const filtered = all.filter((e) =>
    (!agent || e.agent_name === agent) &&
    (!taskId || String(e.task_id) === taskId) &&
    (!type || e.event_type === type) &&
    (!search || (e.summary || "").toLowerCase().includes(search.toLowerCase()))
  );

  useEffect(() => {
    if (follow && bottom.current) bottom.current.scrollIntoView({ block: "end" });
  }, [filtered.length, follow]);

  async function loadOlder() {
    const first = all.length ? all[0].id : 0;
    const rows = await api(withProject(`/api/events?limit=500${first ? `&before_id=${first}` : ""}`, project));
    setOlder((current) => [...rows, ...current]);
  }

  return (
    <div>
      <div className="filters">
        <select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Filter by agent">
          {AGENTS.map((a) => <option key={a} value={a}>{a || "all agents"}</option>)}
        </select>
        <input placeholder="task id" value={taskId} onChange={(e) => setTaskId(e.target.value.replace(/\D/g, ""))} aria-label="Filter by task" />
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by event type">
          <option value="">all events</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input placeholder="search" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search" />
        <label><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow</label>
        <button onClick={loadOlder}>Load older history</button>
        <span className="hint">{filtered.length} / {all.length} events</span>
      </div>
      <div className="console" role="log" aria-label="Live console">
        {filtered.map((e) => {
          const meta = e.metadata || {};
          const isError = /FAIL|ERROR|DENIED|BLOCKED/.test(e.event_type) || e.status === "ERROR";
          return (
            <div key={e.id} className={`line ${isError ? "err" : ""} ${e.actual ? "" : "inferred"}`}>
              <span className="ts">{time(e.ts)}</span>
              <span className="ag">{e.agent_name || e.source}</span>
              <span className="tk">{e.task_id ? `#${e.task_id}` : ""}</span>
              <span className="ev">{e.event_type}</span>
              {meta.tool && <span className="tool">[{meta.tool}]</span>}
              <span className="msg">{e.summary}</span>
              {!e.actual && <span className="hint"> (inferred)</span>}
            </div>
          );
        })}
        <div ref={bottom} />
      </div>
    </div>
  );
}
