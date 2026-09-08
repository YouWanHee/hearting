import assert from "node:assert/strict"
import childProcess from "node:child_process"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { syncBuiltinESMExports } from "node:module"
import { after, test } from "node:test"

// Exercise the real plugin callbacks while replacing process boundaries. Even
// the legitimate-root case may only construct argv; no real tool or store runs.
const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "opencode-cwd-"))
const savedEnv = { ...process.env }
for (const key of Object.keys(process.env)) {
  if ((key.startsWith("AGENT_DISPATCH_") && key !== "AGENT_DISPATCH_JOBS") ||
      key.startsWith("AGENT_ROUTE_") ||
      ["AGENT_SESSION_ROLE", "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"].includes(key)) {
    delete process.env[key]
  }
}
for (const key of ["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "MEM_STORE", "MEM_PROJECTS",
                   "MEM_RECALL_EVENTS", "MEM_RECALL_RECEIPTS", "MEM_WRITE_EVENTS", "AGENT_MODEL_GOVERNOR_ROOT"]) {
  process.env[key] = path.join(fixture, key.toLowerCase())
}
process.env.AGENT_HOME = fixture
fs.mkdirSync(path.join(fixture, "core"), { recursive: true })
fs.mkdirSync(path.join(fixture, "adapters/opencode/bin"), { recursive: true })
fs.writeFileSync(path.join(fixture, "core/CORE.md"), "private callback fixture\n")
const preflight = path.join(fixture, "adapters/opencode/bin/preflight.sh")
fs.writeFileSync(preflight, "#!/bin/sh\nexit 0\n", { mode: 0o700 })
const calls = []
const originals = { spawn: childProcess.spawn, spawnSync: childProcess.spawnSync }
childProcess.spawnSync = (command, args, options) => {
  calls.push({ command, args, options })
  return { status: 0, stdout: "fixture context\n", stderr: "" }
}
childProcess.spawn = (command, args, options) => {
  calls.push({ command, args, options })
  return { unref() {} }
}
syncBuiltinESMExports()
const { AgentHarnessGuards } = await import("../plugins/hearting-guards.js")
after(() => {
  Object.assign(childProcess, originals)
  syncBuiltinESMExports()
  for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
  Object.assign(process.env, savedEnv)
  fs.rmSync(fixture, { recursive: true, force: true })
})

async function verifyContext(label, context, expected) {
  calls.length = 0
  const hooks = await AgentHarnessGuards(context)
  const sid = "cwd-" + label
  await hooks["chat.message"]({ sessionID: sid, messageID: sid + "-turn" }, {
    message: { id: sid + "-turn", sessionID: sid },
    parts: [{ type: "text", text: "Find a stored deployment decision" }],
  })
  await hooks["experimental.chat.system.transform"]({ sessionID: sid }, { system: [] })
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: sid } } })
  const commands = calls.filter(row => row.command === preflight).map(row => row.args)
  assert.deepEqual(commands.find(args => args[0] === "memory"), ["memory", expected])
  assert.deepEqual(commands.find(args => args[0] === "candidates"),
    ["candidates", "Find a stored deployment decision", expected, sid, sid + "-turn"])
  assert.deepEqual(commands.find(args => args[0] === "session-end"), ["session-end", expected, sid])
  assert.equal(commands.filter(args => args[0] === "session-end").length, 1)
}

const projectA = path.join(fixture, "project-a")
const projectB = path.join(fixture, "project-b")
test("native non-Git global root scopes prompt and idle to the opened directory", () =>
  verifyContext("nongit-a", { project: { id: "global", worktree: "/" }, worktree: "/", directory: projectA }, projectA))
test("two non-Git directories remain distinct memory scopes", () =>
  verifyContext("nongit-b", { project: { id: "global", worktree: "/" }, worktree: "/", directory: projectB }, projectB))
test("opening actual slash preserves slash", () =>
  verifyContext("actual-root", { project: { id: "global", worktree: "/" }, worktree: "/", directory: "/" }, "/"))
test("Git subdirectory retains native worktree root", () =>
  verifyContext("git", { project: { id: "git-main", vcs: "git" }, worktree: projectA, directory: path.join(projectA, "src") }, projectA))
test("Git linked worktree retains the supplied canonical mapping", () =>
  verifyContext("linked", { project: { id: "git-main", vcs: "git" }, worktree: projectA, directory: projectB }, projectA))
test("a real Git repository rooted at slash is not a global sentinel", () =>
  verifyContext("git-root", { project: { id: "git-root", vcs: "git" }, worktree: "/", directory: projectA }, "/"))
test("missing worktree still uses directory", () =>
  verifyContext("missing-worktree", { project: { id: "global" }, directory: projectA }, projectA))
test("absent directory retains the process cwd fallback", () =>
  verifyContext("missing-directory", { project: { id: "global" }, worktree: "/" }, process.cwd()))
