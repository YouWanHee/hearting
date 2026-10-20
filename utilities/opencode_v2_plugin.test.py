#!/usr/bin/env python3
"""Hermetic registration and D-42 regression for the OpenCode V2 plugin API."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# A test run inside a registered worker must not lend the fixture its markers.
WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                  "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "AGENT_DISPATCH_REGISTERED_WORKER")
BASE_ENV = {key: value for key, value in os.environ.items() if key not in WORKER_MARKERS}


class OpenCodeV2PluginTest(unittest.TestCase):
    def test_peer_prompt_write_boundary_orders_reserve_recheck_begin_and_no_reply(self):
        source = (ROOT / "adapters/opencode/plugins/hearting-guards.js").read_text()
        start = source.index('const reserveStarted = performance.now()',
                             source.index('async function pendingPeerDelivery(ctx, sid)'))
        end = source.index('pendingPeerTrace(ctx, sid, "noReply-save"', start)
        delivery = source[start:end]
        reserve = delivery.index('["--reserve", row.ref')
        root_recheck = delivery.index('if (!pendingPeerRoot(ctx, sid)) return "native-origin-changed"')
        begin_write = delivery.index('["--begin-write", row.ref')
        sdk_write = delivery.index('client.prompt({ path: { id: sid }')
        self.assertLess(reserve, root_recheck)
        self.assertLess(root_recheck, begin_write)
        self.assertLess(begin_write, sdk_write)
        self.assertIn('noReply: true', delivery[sdk_write:])

    def test_v1_reinjects_contract_without_main_lifecycle_or_native_child_leak(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
import assert from "node:assert/strict"
import { pathToFileURL } from "node:url"
const { AgentHarnessGuards } = await import(pathToFileURL(process.env.PLUGIN_PATH))
const hooks = await AgentHarnessGuards({
  directory: process.env.HOME,
  client: { session: { get: async ({ path }) => ({ data: {
    id: path.id, ...(path.id === "child" ? { parentID: "worker" } : {})
  } }) } }
})
const output = { system: ["native instructions"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "worker" }, output)
assert.deepEqual(output.system, ["native instructions"], "auxiliary system hook received a terminal instruction")
const original = { info: { id: "msg_user", sessionID: "worker", role: "user" },
                   parts: [{ type: "text", text: "summary without terminal fields" }] }
const primary = { messages: [original] }
await hooks["experimental.chat.messages.transform"]({}, primary)
assert.equal(original.parts.length, 1, "the row shared with title generation was mutated")
const contract = primary.messages[0].parts.at(-1).text
assert.match(contract, /verdict: PASS \| FAIL \| BLOCKED/)
assert.match(contract, /runtime_wait: registered-children/)
for (let i = 0; i < 2; i++) {
  const compacted = { messages: [original] }
  await hooks["experimental.chat.messages.transform"]({}, compacted)
  await hooks["experimental.chat.messages.transform"]({}, compacted)
  assert.equal(compacted.messages[0].parts.length, 2)
  assert.equal(compacted.messages[0].parts.at(-1).text, contract)
  const summary = { context: [] }
  await hooks["experimental.session.compacting"]({ sessionID: "worker" }, summary)
  await hooks["experimental.session.compacting"]({ sessionID: "worker" }, summary)
  assert.equal(summary.context.length, 1)
  assert.ok(summary.context[0].endsWith(contract))
}
for (const input of [{ sessionID: "child" }, {}]) {
  const unrelated = { system: [] }
  await hooks["experimental.chat.system.transform"](input, unrelated)
  assert.deepEqual(unrelated.system, [])
}
const child = { messages: [{ info: { id: "msg_child", sessionID: "child", role: "user" }, parts: [] }] }
await hooks["experimental.chat.messages.transform"]({}, child)
assert.deepEqual(child.messages[0].parts, [])
hooks.dispose?.({ silent: true })
const fs = await import("node:fs")
assert.deepEqual(fs.readdirSync(process.env.HOME, { recursive: true }), [],
  "worker hooks wrote main lifecycle state")
'''
            env = {**BASE_ENV, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "HOME": tmp, "AGENT_HOME": str(ROOT), "AGENT_SESSION_ROLE": "worker",
                   "AGENT_DISPATCH_REGISTERED_WORKER": "1", "AGENT_DISPATCH_STAGE_AUTHORITY": "1",
                   "AGENT_DISPATCH_SUBSESSION_ID": "", "PYTHONDONTWRITEBYTECODE": "1",
                   "XDG_STATE_HOME": tmp + "/.state", "XDG_CONFIG_HOME": tmp + "/.config",
                   "XDG_DATA_HOME": tmp + "/.data", "XDG_CACHE_HOME": tmp + "/.cache"}
            run = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_response_telemetry_does_not_block_the_original_stream(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the V2 plugin module contract")
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
import assert from "node:assert/strict"
import childProcess from "node:child_process"
import { syncBuiltinESMExports } from "node:module"
import { pathToFileURL } from "node:url"
let recorded
const calls = []
childProcess.spawn = (command, args) => ({
  on() {},
  stdin: {
    on() {},
    end(body) { calls.push({ command, args, body }); recorded?.() },
  },
})
syncBuiltinESMExports()
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
const hooks = new Map()
const ctx = {
  location: { directory: process.env.TEMP_ROOT },
  session: { hook: async (name, fn) => hooks.set(name, fn) },
  tool: { hook: async () => {} },
  shell: { hook: async () => {} },
  permission: { hook: async () => {} },
  event: { subscribe: async function* ({ signal }) {
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
  } },
}
const cleanup = await plugin.setup(ctx)
try {
  const hook = hooks.get("http.response")
  assert.equal(typeof hook, "function")
  for (const status of [200, 403]) for (const size of [32, 65537]) {
    let controller
    const bytes = new Uint8Array(size).fill(65)
    const response = new Response(new ReadableStream({ start(value) {
      controller = value
      if (size > 65536) value.enqueue(bytes)
    } }), { status, headers: { "content-type": "text/event-stream" } })
    let clones = 0
    const clone = response.clone.bind(response)
    response.clone = () => { clones++; return clone() }
    let returned = false
    const observed = new Promise((resolve) => { recorded = resolve })
    const running = Promise.resolve(hook({
      sessionID: "ses_streamfixture", model: { providerID: "fixture", id: "stream" }, response,
    })).then(() => { returned = true })
    await new Promise((resolve) => setImmediate(resolve))
    assert.equal(returned, true, "telemetry held the provider callback before the original consumer")
    assert.equal(response.bodyUsed, false, "telemetry consumed the original body")
    assert.equal(clones, status === 200 ? 0 : 1, "successful streams must not be cloned")
    const reader = response.body.getReader()
    const first = reader.read()
    if (size <= 65536) controller.enqueue(bytes)
    assert.deepEqual((await first).value, bytes, "the original consumer did not receive its first chunk")
    controller.close()
    assert.equal((await reader.read()).done, true)
    await running
    let timeout
    try {
      await Promise.race([observed, new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("provider observation was lost")), 1000)
      })])
    } finally { clearTimeout(timeout) }
    const call = calls.at(-1)
    assert.equal(call.command, "python3")
    assert.equal(call.args[1], "record-provider-response")
    assert.deepEqual(call.args.slice(2), ["--model", "fixture/stream", "--status", String(status)])
    assert.equal(call.body.byteLength, status === 200 ? 0 : Math.min(size, 65536))
  }
  const stalled = new Response(new ReadableStream(), { status: 403 })
  let timeout
  try {
    const observed = new Promise((resolve) => { recorded = resolve })
    await hooks.get("http.response")({
      sessionID: "ses_streamfixture", model: { providerID: "fixture", id: "stream" }, response: stalled,
    })
    await Promise.race([observed, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error("error observation waited indefinitely")), 2000)
    })])
    assert.equal(calls.at(-1).body.byteLength, 0)
  } finally {
    clearTimeout(timeout)
    await stalled.body.cancel()
  }
  assert.equal(calls.length, 5, "provider observations were duplicated")
} finally { await cleanup() }
'''
            env = {**os.environ,
                   "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": tmp, "AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_v2_registers_guards_and_worker_hooks_are_silent(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable for the V2 plugin module contract")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            home.mkdir()
            project.mkdir()
            script = r'''
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { pathToFileURL } from "node:url"
const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH))).default
assert.equal(plugin.id, "hearting-guards")
assert.equal(typeof plugin.server, "function")
const hooks = new Map()
const sessionID = "ses_workerfixture"
let modelCalls = 0
let appLogs = 0
const controller = new AbortController()
const ctx = {
  location: { directory: process.env.TEMP_ROOT, project: { id: "fixture" } },
  app: { log: async () => { appLogs++ } },
  session: {
    get: async ({ sessionID: sid }) => ({
      id: sid, agent: "build",
      title: { ses_main: "Main work", ses_titler: "hearting-worker-hearting-fleet-titler" }[sid]
        || "hearting-worker-att-fixture",
      // Native 2.0.32 session info carries parentID, null for a root session.
      parentID: sid === "ses_child" ? sessionID : null,
      location: { directory: process.env.TEMP_ROOT },
    }),
    prompt: async () => { modelCalls++ },
    hook: async (name, fn) => hooks.set("session." + name, fn),
  },
  tool: { hook: async (name, fn) => hooks.set("tool." + name, fn) },
  shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
  event: {
    subscribe: async function* ({ signal }) {
      await new Promise((resolve) => setTimeout(resolve, 5))
      yield { type: "session.idle", data: { sessionID } }
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }))
    },
  },
  permission: { hook: async (name, fn) => hooks.set("permission." + name, fn) },
}
const cleanup = await plugin.setup(ctx)
for (const name of ["tool.execute.before", "tool.execute.after", "shell.create.before",
                    "session.prompt", "session.context", "session.compaction"]) {
  assert.equal(typeof hooks.get(name), "function", "missing V2 hook " + name)
}
const prompt = hooks.get("session.prompt")
await prompt({ sessionID, messageID: "msg_fixture", agent: "build",
               prompt: { text: "worker prompt" } })
const context = { sessionID, system: [], messages: [], options: {}, tools: {} }
await hooks.get("session.context")(context)
await new Promise((resolve) => setTimeout(resolve, 20))
assert.equal(modelCalls, 0, "worker hook started another model request")
assert.equal(appLogs, 0, "worker lifecycle emitted state/log writes")
assert.equal(context.system.length, 1)
const contract = context.system[0].text
assert.match(contract, /artifact: <canonical path/)
assert.match(contract, /verdict: PASS \| FAIL \| BLOCKED/)
assert.match(contract, /blocker: none/)
assert.match(contract, /runtime_wait: registered-children/)
assert.doesNotMatch(contract, /Portable Worker Kernel|memory lifecycle/)
// A summary can forget every field; each subsequent primary request still
// receives the exact canonical contract, without repeated accumulation.
for (let i = 0; i < 2; i++) {
  const compacted = { sessionID, system: [], messages: [{ role: "user", content: "summary without fields" }] }
  await hooks.get("session.context")(compacted)
  await hooks.get("session.context")(compacted)
  assert.deepEqual(compacted.system, [{ type: "text", text: contract }])
  const summary = { sessionID, system: [] }
  await hooks.get("session.compaction")(summary)
  await hooks.get("session.compaction")(summary)
  assert.equal(summary.system.length, 1)
  assert.ok(summary.system[0].text.endsWith(contract))
  assert.match(summary.system[0].text, /normal compaction summary/)
  assert.equal(summary.context, undefined, "V1 context leaked into the V2 event")
}
assert.equal(modelCalls, 0)
assert.equal(appLogs, 0)
assert.deepEqual(fs.readdirSync(process.env.TEMP_ROOT, { recursive: true }), [],
  "worker lifecycle wrote project state")
assert.deepEqual(fs.readdirSync(process.env.HOME, { recursive: true }), [],
  "worker lifecycle wrote user state")
// Neighbors in the shared service: a native child, a fleet helper session and
// a main session, whose own normal lifecycle may write main state.
for (const sid of ["ses_child", "ses_titler", "ses_main"]) {
  const unrelated = { sessionID: sid, system: [] }
  await hooks.get("session.context")(unrelated)
  assert.ok(!unrelated.system.some((part) => part.text?.includes(contract)), "worker contract leaked to " + sid)
  const summary = { sessionID: sid, system: [] }
  await hooks.get("session.compaction")(summary)
  // Main V2 compaction keeps the native summary template untouched.
  assert.deepEqual(summary.system, [], "compaction instructions leaked to " + sid)
}
await cleanup()
'''
            env = {**BASE_ENV, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": str(project), "HOME": str(home), "AGENT_HOME": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1", "XDG_STATE_HOME": str(home / ".state"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".data"),
                   "XDG_CACHE_HOME": str(home / ".cache")}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
