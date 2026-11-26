import React, { useState } from "react";

const STATUS_CLASS = {
  PASS: "ok", COMPLETED: "ok", HEALTHY: "ok", ACTIVE: "info", RUNNING: "info", IN_PROGRESS: "info", live: "ok",
  FAIL: "bad", FAILED: "bad", ERROR: "bad", BLOCKED: "warn", BLOCKING: "bad", PAUSED: "warn", STOPPING: "warn",
  PENDING: "muted", ON_HOLD: "warn", PROPOSED: "warn", OPEN: "bad", FIXED: "ok", VERIFIED: "ok", APPROVED: "ok",
  CRITICAL: "bad", HIGH: "bad", MEDIUM: "warn", LOW: "muted", INFO: "muted", IDLE: "muted", STOPPED: "muted",
  NOT_APPLICABLE: "muted", SKIPPED: "muted", reconnecting: "warn", connecting: "warn",
};

export function Badge({ value }) {
  if (value === null || value === undefined || value === "") return <span className="badge muted">-</span>;
  return <span className={`badge ${STATUS_CLASS[value] || "muted"}`}>{String(value)}</span>;
}

export function Card({ title, value, sub, children }) {
  return (
    <div className="card">
      {title && <div className="card-title">{title}</div>}
      {value !== undefined && <div className="card-value">{value}</div>}
      {sub && <div className="card-sub">{sub}</div>}
      {children}
    </div>
  );
}

export function Section({ title, actions, children }) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        <div>{actions}</div>
      </div>
      {children}
    </section>
  );
}

export function Table({ columns, rows, onRowClick, empty = "Nothing to show yet." }) {
  if (!rows || rows.length === 0) return <p className="empty">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((c) => <th key={c.key}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row.id ?? i} onClick={onRowClick ? () => onRowClick(row) : undefined} className={onRowClick ? "clickable" : ""}>
              {columns.map((c) => <td key={c.key}>{c.render ? c.render(row) : formatCell(row[c.key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function JsonView({ value }) {
  const [open, setOpen] = useState(false);
  if (value === null || value === undefined) return null;
  return (
    <div>
      <button className="link" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} details</button>
      {open && <pre className="json">{JSON.stringify(value, null, 2)}</pre>}
    </div>
  );
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error-box">{error.message || String(error)}</div>;
}

export function Modal({ title, onClose, children }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="section-head">
          <h2>{title}</h2>
          <button onClick={onClose} aria-label="Close">x</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function time(value) {
  if (!value) return "-";
  const d = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  return isNaN(d) ? value : d.toLocaleString();
}
