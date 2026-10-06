// Hearting TUI identity provenance — target-only TUI entry, never server.
//
// Reads this TUI's actual current session selection from the native route and
// records it in a scoped runtime-state file. The server identity publisher
// verifies that record against the same process lifecycle (pid + start) and
// the exact session id before its own SDK check; this entry publishes nothing
// itself: no identity report, no peer write, no client calls. Routes without
// a selected session (home, custom, unknown) and teardown remove the record,
// so a blank home never claims an identity.
//
// Plain JS (no type annotations) so the regression fixture can execute it
// outside the TUI host; Bun loads it as a target-only module.

import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import path from "node:path"

const PLUGIN_ID = "hearting.tui-identity"
const RECORD_SCHEMA = "hearting-tui-selection-v1"
const RECORD_SUBDIR = ["hearting", "tui-identity"]
// Verified SDK v2 event names (packages/sdk/js v2 gen types, pinned 1.18.34).
// Bus traffic is wake-assist only: native navigation itself emits no bus
// event (create waits ~50ms before route.navigate; select navigates
// directly), so handlers below only re-read the live native getter. The
// primary observation is the host-driven slot render further down, which
// runs synchronously with the mounted route.
const REFRESH_EVENTS = ["session.created", "session.deleted", "tui.session.select",
  "session.next.prompted", "message.updated", "session.status", "session.idle"]

function cleanSid(value) {
  if (typeof value !== "string" || !value || value.length > 256 || /[\x00-\x1f\x7f]/.test(value)) return null
  return value
}

function recordDir(env) {
  const home = (env && env.HOME) || ""
  const base = (env && env.XDG_STATE_HOME) || (home ? path.join(home, ".local", "state") : "")
  if (!base) return ""
  return path.join(base, ...RECORD_SUBDIR)
}

function ownLifecycle() {
  // Same lifecycle proof the server publisher uses: the start time guards
  // PID reuse, so a record can only ever match its own process.
  try {
    const pid = typeof process !== "undefined" ? process.pid : 0
    if (!Number.isInteger(pid) || pid <= 1) return null
    const stat = readFileSync("/proc/self/stat", "utf8")
    const body = stat.slice(stat.lastIndexOf(") ") + 2)
    if (stat.lastIndexOf(") ") < 0) return null
    const fields = body.trim().split(/\s+/)
    const start = fields[19]
    const self = Number(stat.slice(0, stat.indexOf(" (")))
    if (self !== pid || !/^\d+$/.test(start || "")) return null
    return { pid, start }
  } catch { return null }
}

function selectedSid(api) {
  try {
    const current = api && api.route && api.route.current
    if (!current || current.name !== "session") return null
    return cleanSid(current.params && current.params.sessionID)
  } catch { return null }
}

function recordPath(dir, own) {
  if (!dir || !own) return ""
  return path.join(dir, own.pid + "-" + own.start + ".json")
}

function syncRecord(api, mem) {
  const env = (typeof process !== "undefined" && process.env) || {}
  const own = ownLifecycle()
  const file = recordPath(recordDir(env), own)
  if (!file || !own) return false
  const sid = selectedSid(api)
  try {
    if (!sid) {
      rmSync(file, { force: true })
      mem.last = ""
      return true
    }
    // Change-gated: steady activity re-reads the same selection without
    // churning the record, so the server's pre-publication generation
    // comparison stays stable until the selection actually moves.
    const key = "S:" + sid
    if (mem.last === key) return true
    mkdirSync(path.dirname(file), { recursive: true })
    const body = JSON.stringify({ schema: RECORD_SCHEMA,
      sessionID: sid, pid: own.pid, start: own.start, writtenAt: new Date().toISOString() })
    const tmp = file + ".tmp-" + own.pid
    writeFileSync(tmp, body, { encoding: "utf8" })
    renameSync(tmp, file)
    mem.last = key
    return true
  } catch { return false }
}

function deletedSid(event) {
  // A deleted session's selection is dead whatever the getter still shows:
  // the event carries the deletion truth, the getter carries this TUI's
  // own route, and only their agreement clears. Anything else is untouched.
  // Official shape (pinned 1.18.34 SDK v2 gen types) names the session at
  // properties.info.id; properties.sessionID is read only as a fallback.
  try {
    if (!event || typeof event.type !== "string") return null
    if (event.type !== "session.deleted") return null
    const props = (event && event.properties) || {}
    const info = props.info || {}
    const sid = cleanSid(info.id) || cleanSid(props.sessionID)
    if (!sid) return null
    return sid
  } catch { return null }
}

function clearRecord() {
  const env = (typeof process !== "undefined" && process.env) || {}
  const file = recordPath(recordDir(env), ownLifecycle())
  if (!file) return false
  try {
    rmSync(file, { force: true })
    return true
  } catch { return false }
}

async function tui(api) {
  const mem = { last: "" }
  const refresh = () => { syncRecord(api, mem) }
  refresh()
  const unsubs = []
  try {
    for (const type of REFRESH_EVENTS) {
      if (type === "session.deleted") unsubs.push(api.event.on(type, (event) => {
        // Deletion truth beats a lagging getter: when the event names the
        // session this TUI still shows, the selection is already dead.
        const gone = deletedSid(event)
        if (gone && selectedSid(api) === gone) clearRecord()
        refresh()
      }))
      else unsubs.push(api.event.on(type, refresh))
    }
  } catch {
    // Partial subscriptions are still disposed below; a broken bus must
    // never block activation.
  }
  // Primary observation: host-driven slot renders run synchronously with
  // the mounted route (verified official in-repo smoke shape: slots record
  // of (ctx, value) render fns; session_prompt_right carries session_id).
  // No bus traffic is needed and none is inferred from: a agreeing
  // session mount writes, a agreeing home mount clears, anything
  // disagreeing is left for the next render or wake. Renders return null
  // and change nothing visible.
  // The app-level render additionally covers routes with no session/home
  // slot (custom plugin routes): it reads only the live getter, so a
  // non-session route clears whatever a previous selection left behind.
  // A render runs inside the host's reactive tree, so a getter read here
  // tracks the native route store and re-runs when the selection moves.
  const slotPlugin = { slots: {
    app_bottom() {
      try {
        const sid = selectedSid(api)
        if (!sid && mem.last !== "") {
          clearRecord()
          mem.last = ""
        } else if (sid) syncRecord(api, mem)
      } catch { /* a broken render must never break the host view */ }
      return null
    },
    session_prompt_right(ctx, value) {
      try {
        const sid = cleanSid(value && value.session_id)
        if (sid && selectedSid(api) === sid) syncRecord(api, mem)
      } catch { /* a broken render must never break the host view */ }
      return null
    },
    home_prompt_right() {
      try {
        if (!selectedSid(api) && mem.last !== "") {
          clearRecord()
          mem.last = ""
        }
      } catch { /* a broken render must never break the host view */ }
      return null
    },
  } }
  try {
    api.slots.register(slotPlugin)
  } catch {
    // Hosts without slot registration keep the bus wake-assist path.
  }
  const done = () => {
    for (const off of unsubs) {
      try { off() } catch { /* teardown is best-effort */ }
    }
    clearRecord()
  }
  try {
    api.lifecycle.onDispose(done)
  } catch {
    // The host still owns teardown; without a disposer the record simply
    // stops being refreshed and never matches a new lifecycle.
  }
}

export default { id: PLUGIN_ID, tui }
