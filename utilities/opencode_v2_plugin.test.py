#!/usr/bin/env python3
"""Hermetic registration and D-42 regression for the OpenCode V2 plugin API."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OpenCodeV2PluginTest(unittest.TestCase):
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
      id: sid, title: "hearting-worker-att-fixture", agent: "build",
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
assert.deepEqual(context.system, [], "worker context received main-session lifecycle injection")
await cleanup()
const projectFiles = fs.readdirSync(process.env.TEMP_ROOT, { recursive: true })
const homeFiles = fs.readdirSync(process.env.HOME, { recursive: true })
assert.deepEqual(projectFiles, [], "worker lifecycle wrote project state")
assert.deepEqual(homeFiles, [], "worker lifecycle wrote user state")
'''
            env = {**os.environ, "PLUGIN_PATH": str(ROOT / "adapters/opencode/plugins/hearting-guards.js"),
                   "TEMP_ROOT": str(project), "HOME": str(home), "AGENT_HOME": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1", "XDG_STATE_HOME": str(home / ".state"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".data"),
                   "XDG_CACHE_HOME": str(home / ".cache")}
            result = subprocess.run([node, "--input-type=module", "-e", script],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
