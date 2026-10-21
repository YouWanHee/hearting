// installed by herdr
// managed by herdr; reinstalling or updating the integration overwrites this file.
// add custom hooks/plugins beside this file instead of editing it.
// HERDR_INTEGRATION_ID=opencode
// HERDR_INTEGRATION_VERSION=13

import net from "node:net";
import { appendFileSync, readFileSync } from "node:fs";
import path from "node:path";

const SOURCE = "herdr:opencode";
const AGENT = "opencode";
let reportSeq = Date.now() * 1000;
let requestChain = Promise.resolve();
let reportedRootSessionID;

// Track child sessions so their events cannot replace the pane's root session.
// User prompts carry the root id to preserve its identity and cross-talk guard.
const childSessions = new Map();
const CHILD_EVENT_STATES = new Map([
  ["permission.asked", "blocked"],
  ["question.asked", "blocked"],
  ["permission.replied", "working"],
  ["question.replied", "working"],
  ["question.rejected", "working"],
]);

function nextReportSeq() {
  reportSeq += 1;
  return reportSeq;
}

function sessionIDFromProperties(properties) {
  return typeof properties?.sessionID === "string" && properties.sessionID
    ? properties.sessionID
    : undefined;
}

function recordLiveObservation(type, properties) {
  // Test-runner evidence is opt-in and ID-only. Never persist question, prompt,
  // message, or draft text from the native SDK event.
  try {
    const root = process.env.HEARTING_HERDR_LIVE_ROOT;
    const target = process.env.HEARTING_HERDR_LIVE_OBSERVATIONS;
    if (!root || !target) return;
    const resolvedRoot = path.resolve(root);
    const resolvedTarget = path.resolve(target);
    const relative = path.relative(resolvedRoot, resolvedTarget);
    if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return;

    const info = properties?.info || {};
    const sessionID = sessionIDFromProperties(properties) ||
      (typeof info.id === "string" ? info.id : undefined);
    if (!sessionID) return;
    let row;
    if (["question.asked", "permission.asked"].includes(type)) {
      if (typeof properties?.id !== "string" || !properties.id) return;
      row = { event: type, session_id: sessionID,
        kind: type.startsWith("question") ? "question" : "permission",
        request_id: properties.id, status: "open" };
    } else if (["question.replied", "question.rejected", "permission.replied"].includes(type)) {
      const id = properties?.requestID;
      if (typeof id !== "string" || !id) return;
      row = { event: type, session_id: sessionID,
        kind: type.startsWith("question") ? "question" : "permission",
        request_id: id, status: type.endsWith("rejected") ? "rejected" : "resolved" };
    } else if (type === "message.updated") {
      const message = properties?.info || properties?.message || {};
      const id = message.id || properties?.id;
      const role = message.role || properties?.role;
      if (typeof id !== "string" || !id || !["user", "assistant"].includes(role)) return;
      row = { event: type, session_id: sessionID, message_id: id, role };
    } else if (type === "session.created") {
      row = { event: type, session_id: sessionID };
    } else {
      return;
    }
    row.pid = process.pid;
    row.at = Date.now();
    appendFileSync(resolvedTarget, JSON.stringify(row) + "\n", { encoding: "utf8", mode: 0o600 });
  } catch { /* Optional live evidence never changes native event handling. */ }
}

const SESSION_STATE_BY_STATUS = new Map([
  ["idle", "idle"],
  ["active", "working"],
  ["busy", "working"],
  ["pending", "working"],
  ["retry", "working"],
  ["running", "working"],
  ["streaming", "working"],
  ["working", "working"],
]);

function stateFromSessionStatus(status) {
  const kind = typeof status === "string" ? status : status?.type;
  return typeof kind === "string"
    ? SESSION_STATE_BY_STATUS.get(kind.toLowerCase())
    : undefined;
}

function nativeResumeWireArgv(argv, sessionID) {
  if (!Array.isArray(argv) || argv.length === 0 || argv.length > 64
      || argv.some((arg) => typeof arg !== "string" || !arg || arg.includes("\0"))
      || argv.reduce((bytes, arg) => bytes + Buffer.byteLength(arg, "utf8"), 0) > 8192) return null;
  const executable = argv[0];
  if (executable !== AGENT && !(path.isAbsolute(executable) && path.basename(executable) === AGENT)) return null;
  if (typeof sessionID !== "string" || !sessionID || sessionID.includes("\0")) return null;
  if (Buffer.byteLength(sessionID, "utf8") + Buffer.byteLength("opencode--session", "utf8") > 8192) return null;
  const args = argv.slice(1), found = [];
  const values = new Set(["--model", "-m", "--agent", "--variant", "--format", "--dir", "-D"]);
  const flags = new Set(["--help", "--version", "--print-logs"]);
  let ambiguous = false, continued = false;
  for (let i = 0; i < args.length;) {
    const token = args[i];
    if (token === "--") { if (i + 1 < args.length) ambiguous = true; break; }
    if (token === "--fork" || token === "--fork=true") return [AGENT, "--session", sessionID];
    if (token.startsWith("--fork=") && token !== "--fork=false") return [AGENT, "--session", sessionID];
    if (token === "--continue" || token === "--last" || token === "-c") continued = true;
    if (token === "--session" || token === "-s") {
      if (i + 1 < args.length && !args[i + 1].startsWith("-")) { found.push([i + 1, ""]); i += 2; }
      else i += 1;
      continue;
    }
    const selector = ["--session", "-s"].find((option) => token.startsWith(`${option}=`));
    if (selector) { found.push([i, `${selector}=`]); i += 1; continue; }
    if (token.startsWith("-")) {
      const key = token.split("=", 1)[0];
      if (values.has(key) && !token.includes("=")) { if (i + 1 < args.length) i += 2; else { ambiguous = true; i += 1; } continue; }
      if (flags.has(key) || token.includes("=")) { i += 1; continue; }
      ambiguous = true; i += 1; continue;
    }
    ambiguous = true; i += 1;
  }
  if (!found.length) return null;
  if (found.length !== 1 || continued || ambiguous) return [AGENT, "--session", sessionID];
  const [index, prefix] = found[0], result = [AGENT, ...args];
  result[index + 1] = `${prefix}${sessionID}`;
  return result.length <= 64 && result.reduce((bytes, arg) => bytes + Buffer.byteLength(arg, "utf8"), 0) <= 8192
    ? result : null;
}

function request(method, params) {
  const pending = requestChain.then(() => requestOnce(method, params));
  requestChain = pending.catch(() => {});
  return pending;
}

function requestOnce(method, params) {
  const paneId = process.env.HERDR_PANE_ID;
  const socketPath = process.env.HERDR_SOCKET_PATH;

  if (!paneId || !socketPath) {
    return Promise.resolve();
  }

  const socketEndpoint =
    process.platform === "win32" ? `\\\\.\\pipe\\${socketPath}` : socketPath;

  const requestId = `${SOURCE}:${Date.now()}:${Math.floor(Math.random() * 1_000_000)
    .toString()
    .padStart(6, "0")}`;
  const request = {
    id: requestId,
    method,
    params: {
      pane_id: paneId,
      source: SOURCE,
      agent: AGENT,
      ...(method === "pane.report_agent_session" ? {} : { seq: nextReportSeq() }),
      ...params,
    },
  };

  return new Promise((resolve) => {
    const client = net.createConnection(socketEndpoint, () => {
      client.write(`${JSON.stringify(request)}\n`);
    });

    const finish = () => {
      client.destroy();
      resolve();
    };

    client.setTimeout(500, finish);
    client.on("data", finish);
    client.on("error", finish);
    client.on("end", finish);
    client.on("close", resolve);
  });
}

function nativeSessionLifecycle(sessionID) {
  try {
    const pid = process.pid;
    if (!Number.isInteger(pid) || pid <= 1) return {};
    const stat = readFileSync("/proc/self/stat", "utf8");
    const close = stat.lastIndexOf(") ");
    if (close < 0 || Number(stat.slice(0, stat.indexOf(" ("))) !== pid) return {};
    const fields = stat.slice(close + 2).trim().split(/\s+/);
    const start = fields[19];
    if (!/^\d+$/.test(start || "")) return {};
    const home = process.env.HOME || "";
    const state = process.env.XDG_STATE_HOME || (home ? path.join(home, ".local", "state") : "");
    if (!state) return {};
    const file = path.join(state, "hearting", "tui-identity", `${pid}-${start}.json`);
    const record = JSON.parse(readFileSync(file, "utf8"));
    if (record?.schema !== "hearting-tui-selection-v1" || record.pid !== pid
        || String(record.start) !== start || record.sessionID !== sessionID) return {};
    const result = {};
    if (Number.isSafeInteger(record.sessionSeq) && record.sessionSeq > 0) {
      result.seq = record.sessionSeq;
    }
    if (["startup", "resume", "select", "clear", "compact", "fork"].includes(record.sessionStartSource)) {
      result.session_start_source = record.sessionStartSource;
    }
    const argv = process.argv;
    const wireArgv = nativeResumeWireArgv(argv, sessionID);
    if (wireArgv) result.resume_argv = wireArgv;
    return result;
  } catch { return {} }
}

function reportSession(sessionID) {
  if (!sessionID) {
    return Promise.resolve();
  }
  return request("pane.report_agent_session", {
    agent_session_id: sessionID,
    ...nativeSessionLifecycle(sessionID),
  });
}

function reportState(state, sessionID) {
  const params = { state };
  if (sessionID) {
    reportedRootSessionID = sessionID;
    params.agent_session_id = sessionID;
  }
  return request("pane.report_agent", params);
}

function ownsLocalLifecycle() {
  const args = process.argv.slice(2);
  const separator = args.indexOf("--");
  if (separator !== -1) args.splice(separator);
  if (args.some((arg) => arg === "--attach" || arg.startsWith("--attach="))) return false;
  while (args[0] === "--print-logs" || args[0] === "--log-level" || args[0]?.startsWith("--log-level=")) {
    args.splice(0, args[0] === "--log-level" ? 2 : 1);
  }
  // These local clients have no TUI plugin. Shared servers and the TUI worker
  // cannot identify their attached panes; their lifecycle belongs to each TUI.
  return args[0] === "run" ||
    (!["serve", "web", "attach"].includes(args[0]) && args.includes("--mini"));
}

export const HerdrAgentStatePlugin = async () => {
  if (
    !ownsLocalLifecycle() ||
    process.env.HERDR_ENV !== "1" ||
    !process.env.HERDR_SOCKET_PATH ||
    !process.env.HERDR_PANE_ID
  ) {
    return {};
  }

  return {
    "chat.message": async ({ sessionID }) => {
      if (sessionID && childSessions.has(sessionID)) {
        return;
      }
      await reportState("working", sessionID);
    },
    event: async ({ event }) => {
      const type = event?.type;
      const properties = event?.properties ?? {};
      recordLiveObservation(type, properties);
      const sessionID = sessionIDFromProperties(properties);

      const info = properties.info;
      if (info?.id && info.parentID) {
        childSessions.set(info.id, info.parentID);
      }
      if (sessionID && childSessions.has(sessionID)) {
        const state = CHILD_EVENT_STATES.get(type);
        if (state) {
          let rootSessionID = sessionID;
          while (childSessions.has(rootSessionID)) {
            rootSessionID = childSessions.get(rootSessionID);
          }
          await reportState(state, rootSessionID);
        }
        return;
      }

      switch (type) {
        case "session.created":
          // Creation is server-global, so an attached client may own it. The
          // TUI plugin separately reports the root selected in this pane.
          reportedRootSessionID = sessionID;
          break;
        case "session.updated":
          if (sessionID && sessionID !== reportedRootSessionID) {
            await reportSession(sessionID);
          }
          break;
        case "session.status": {
          const state = stateFromSessionStatus(properties.status);
          if (state) {
            await reportState(state, sessionID);
          } else {
            await reportSession(sessionID);
          }
          break;
        }
        case "tool.execute.before":
        case "tool.execute.after":
        case "permission.replied":
        case "question.replied":
        case "question.rejected":
        case "session.compacted":
          await reportState("working", sessionID);
          break;
        case "permission.asked":
        case "question.asked":
        case "session.error":
          await reportState("blocked", sessionID);
          break;
        case "session.idle":
          await reportState("idle", sessionID);
          break;
        case "session.deleted":
          break;
        default:
          break;
      }
    },
  };
};

// V1 local run/Mini retain their server hooks. V1/V2 full TUIs own both
// selection and lifecycle, including when attached to a shared remote server.
export default {
  id: "herdr.opencode",
  server: HerdrAgentStatePlugin,
  setup() {},
};
