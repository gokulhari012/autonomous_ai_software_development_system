// Small fetch wrapper: same-origin cookies, CSRF header for state-changing requests.
function csrfToken() {
  const match = document.cookie.match(/(?:^|; )autodev_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    throw new ApiError(response.status, (data && (data.detail || data.message)) || response.statusText);
  }
  return data;
}

export function withProject(path, project) {
  if (!project) return path;
  return path + (path.includes("?") ? "&" : "?") + "project=" + encodeURIComponent(project);
}

export function artifactUrl(path) {
  return "/api/artifact?path=" + encodeURIComponent(path);
}
