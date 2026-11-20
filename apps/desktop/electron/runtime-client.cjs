const { execFile } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");

const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);

const API_BASE = "http://127.0.0.1:8787";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const TASK_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const REQUEST_TIMEOUT_MS = 5000;
const TOKEN_TIMEOUT_MS = 5000;
const MAX_RESPONSE_BYTES = 1 << 20;

let cachedToken = null;
let pendingToken = null;

class RuntimeError extends Error {
  constructor(message, kind = "error") {
    super(message);
    this.name = "RuntimeError";
    this.kind = kind;
  }
}

function resolveAgentBinary() {
  const executable = process.platform === "win32" ? "agent.exe" : "agent";
  const candidates = [];
  if (process.env.CRANE_AGENT_BINARY) {
    candidates.push(path.resolve(process.env.CRANE_AGENT_BINARY));
  }
  if (process.resourcesPath) {
    candidates.push(path.join(process.resourcesPath, executable));
  }
  candidates.push(path.join(__dirname, "../../../", executable));
  candidates.push(path.join(process.cwd(), executable));
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

async function readToken() {
  if (cachedToken) return cachedToken;
  if (pendingToken) return pendingToken;

  pendingToken = (async () => {
    const binary = resolveAgentBinary();
    if (!binary) {
      throw new RuntimeError("Go runtime executable was not found", "disconnected");
    }
    try {
      const { stdout } = await execFileAsync(binary, ["api-token"], {
        timeout: TOKEN_TIMEOUT_MS,
        windowsHide: true,
        maxBuffer: 64 * 1024,
      });
      const token = String(stdout).trim();
      if (!TOKEN_PATTERN.test(token)) {
        throw new RuntimeError("Go runtime returned an invalid installation token", "error");
      }
      cachedToken = token;
      return token;
    } catch (error) {
      if (error instanceof RuntimeError) throw error;
      const kind = error && (error.code === "ENOENT" || error.code === "ETIMEDOUT")
        ? "disconnected"
        : "error";
      // Never include stdout, stderr, arguments or paths in the error text.
      throw new RuntimeError("Unable to read the protected installation token", kind);
    }
  })();

  try {
    return await pendingToken;
  } finally {
    pendingToken = null;
  }
}

async function apiGet(endpoint, allowTokenRetry = true) {
  const token = await readToken();
  let response;
  try {
    response = await fetch(`${API_BASE}${endpoint}`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
      redirect: "manual",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    throw new RuntimeError("Local supervisor is unreachable", "disconnected");
  }

  if (response.status === 401 && allowTokenRetry) {
    cachedToken = null;
    return apiGet(endpoint, false);
  }

  const contentLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_RESPONSE_BYTES) {
    if (response.body) void response.body.cancel();
    throw new RuntimeError("Supervisor response was too large", "error");
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new RuntimeError("Supervisor returned an invalid JSON response", "error");
  }

  if (!response.ok) {
    const code = payload && payload.error && typeof payload.error.code === "string"
      ? payload.error.code
      : "request_failed";
    if (response.status === 404) {
      throw new RuntimeError("Requested data was not found", "not_found");
    }
    throw new RuntimeError(`Supervisor request failed (${code})`, "error");
  }
  if (!payload || typeof payload !== "object" || !payload.data || typeof payload.data !== "object") {
    throw new RuntimeError("Supervisor returned an unexpected response", "error");
  }
  return payload.data;
}

async function getHealth() {
  const data = await apiGet("/v1/health");
  if (typeof data.ready !== "boolean" || typeof data.supervisor !== "boolean" ||
      typeof data.persistence !== "boolean" || !data.runtime ||
      typeof data.runtime.available !== "boolean") {
    throw new RuntimeError("Supervisor returned an incomplete health record", "error");
  }
  return data;
}

async function getTasks() {
  const data = await apiGet("/v1/tasks");
  if (!Array.isArray(data.tasks)) {
    throw new RuntimeError("Supervisor returned an invalid task list", "error");
  }
  return data;
}

async function getTask(id) {
  if (typeof id !== "string" || !TASK_ID_PATTERN.test(id)) {
    throw new RuntimeError("Invalid task identifier", "bad_request");
  }
  const data = await apiGet(`/v1/tasks/${encodeURIComponent(id)}`);
  if (!data.task || typeof data.task !== "object") {
    throw new RuntimeError("Supervisor returned an invalid task detail", "error");
  }
  return data.task;
}

module.exports = { getHealth, getTasks, getTask };
