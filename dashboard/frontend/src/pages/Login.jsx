import React, { useState } from "react";
import { api } from "../api.js";
import { ErrorBox } from "../components.jsx";

export default function Login({ onLogin }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);

  async function submit(event) {
    event.preventDefault();
    try {
      const result = await api("/api/auth/login", { method: "POST", body: { username, password } });
      onLogin({ username: result.username, csrf: result.csrf });
    } catch (err) {
      setError(err);
    }
  }

  return (
    <div className="center">
      <form className="login" onSubmit={submit}>
        <h1>Autonomous Development Dashboard</h1>
        <label htmlFor="username">Username</label>
        <input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        <button type="submit">Sign in</button>
        <ErrorBox error={error} />
      </form>
    </div>
  );
}
