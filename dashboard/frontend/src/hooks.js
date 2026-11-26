import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// Fetch a REST endpoint and refresh it periodically (the database is the source of truth).
export function usePoll(path, intervalMs = 5000, enabled = true) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(async () => {
    if (!path || !enabled) return;
    try {
      setData(await api(path));
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [path, enabled]);
  useEffect(() => {
    load();
    if (!intervalMs) return undefined;
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
  }, [load, intervalMs]);
  return { data, error, reload: load };
}

// Live event stream over WebSocket with automatic reconnection. On reconnect the
// last received event id is sent, and the server replays everything missed.
export function useEventStream(project, maxEvents = 3000) {
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState("connecting");
  const lastId = useRef(0);
  const socketRef = useRef(null);

  useEffect(() => {
    let closed = false;
    let retry = 0;
    let timer = null;
    setEvents([]);
    lastId.current = 0;

    function connect() {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const query = new URLSearchParams({ after_id: String(lastId.current) });
      if (project) query.set("project", project);
      const ws = new WebSocket(`${scheme}://${window.location.host}/ws/events?${query}`);
      socketRef.current = ws;
      setStatus("connecting");
      ws.onopen = () => {
        retry = 0;
        setStatus("live");
        ws.send(JSON.stringify({ type: "subscribe", project: project || null, after_id: lastId.current }));
      };
      ws.onmessage = (message) => {
        let payload;
        try {
          payload = JSON.parse(message.data);
        } catch {
          return;
        }
        if (payload.type === "events" && payload.events.length) {
          lastId.current = payload.last_id;
          setEvents((current) => {
            const known = new Set(current.map((e) => e.id));
            const merged = current.concat(payload.events.filter((e) => !known.has(e.id)));
            return merged.length > maxEvents ? merged.slice(merged.length - maxEvents) : merged;
          });
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setStatus("reconnecting");
        retry += 1;
        timer = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(retry, 5)));
      };
      ws.onerror = () => ws.close();
    }

    connect();
    const ping = setInterval(() => {
      if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: "ping" }));
    }, 25000);
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(ping);
      if (socketRef.current) socketRef.current.close();
    };
  }, [project, maxEvents]);

  return { events, status };
}
