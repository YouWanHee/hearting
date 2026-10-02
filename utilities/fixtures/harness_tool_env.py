"""Tool-command environments each harness runtime produces, built from the shipped repo config.

A harness clears what it inherited from another harness in the environment of its own tool
commands and leaves its own native session id as the only identity evidence (`core/OPERATIONS.md`,
"A harness runtime clears inherited caller names"). Nothing a harness exports names a harness, so
nothing it exports can go stale in a grandchild. This helper applies each installed config surface
the way its runtime does, so a test can ask "what do the resolvers see in a Codex thread served by a
daemon that a Claude shell started?" without launching any harness:

- claude: `adapters/claude/settings.json` `env` overwrites the inherited shell values with blanks,
  and Claude exports its own session id.
- codex: `adapters/codex/config/harness-identity.toml` (`shell_environment_policy.filters`):
  exclusions drop inherited keys, and Codex adds its own thread id per command.
- opencode: the `shell.env` hook of `hearting-guards.js` is merged over the inherited env and sets
  the session id (the plugin is OpenCode's only source of one).

``installed=False`` leaves the config surface out (a partially activated host); the harness's own
runtime still exports what it always does.

A missing config file or key counts as an empty declaration, so a test fails on its assertion
rather than on import before the config exists.
"""
from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "adapters" / "claude" / "settings.json"
CODEX_FRAGMENT = ROOT / "adapters" / "codex" / "config" / "harness-identity.toml"
OPENCODE_PLUGIN = ROOT / "adapters" / "opencode" / "plugins" / "hearting-guards.js"

HARNESSES = ("claude", "codex", "opencode")
IDENTITY_KEYS = (
    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID",
)
# The native session id variable each harness's own runtime supplies.
NATIVE_SESSION_KEY = {
    "claude": "CLAUDE_CODE_SESSION_ID",
    "codex": "CODEX_THREAD_ID",
    "opencode": "OPENCODE_SESSION_ID",
}
_FOREIGN_SESSION_KEYS = {
    "claude": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
    "opencode": ("OPENCODE_SESSION_ID",),
}


class ToolMissing(Exception):
    """An optional tool (node) is absent; tests translate this to `SkipTest`."""


def claude_env_declaration() -> dict:
    try:
        env = json.loads(SETTINGS.read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError):
        return {}
    return dict(env) if isinstance(env, dict) else {}


def codex_policy() -> dict:
    """`{"set": {...}, "exclude": [patterns]}` read from the Codex fragment (`set` is empty by design)."""
    import tomllib

    try:
        data = tomllib.loads(CODEX_FRAGMENT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"set": {}, "exclude": []}
    policy = data.get("shell_environment_policy")
    policy = policy if isinstance(policy, dict) else {}
    filters = policy.get("filters")
    filters = filters if isinstance(filters, dict) else {}
    return {
        "set": dict(policy.get("set") or {}),
        "exclude": [key for key, action in filters.items() if action == "exclude"],
    }


def opencode_shell_env(session_id: str | None) -> dict | None:
    """Run the real plugin `shell.env` hook under node. ``None`` when node is missing."""
    node = shutil.which("node")
    if node is None:
        return None
    script = (
        f"import {{ AgentHarnessGuards }} from {json.dumps(OPENCODE_PLUGIN.as_uri())}\n"
        "const plugin = await AgentHarnessGuards({ directory: process.cwd(), worktree: process.cwd() })\n"
        "const output = {}\n"
        f"await plugin['shell.env']({{ cwd: process.cwd(), sessionID: {json.dumps(session_id)} }}, output)\n"
        "console.log(JSON.stringify(output.env || {}))\n"
    )
    proc = subprocess.run([node, "--input-type=module"], input=script, capture_output=True,
                          text=True, timeout=60, cwd=str(ROOT))
    if proc.returncode != 0:
        raise RuntimeError(f"opencode shell.env probe failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def daemon_started_from(env: dict) -> dict:
    """A shared service (the Codex app-server daemon) inherits the env of whatever started it."""
    return dict(env)


def tool_shell_env(harness: str, inherited: dict, sid: str, installed: bool = True) -> dict:
    """The env the `harness` runtime gives its tool commands, given the env it inherited."""
    env = dict(inherited)
    if harness == "claude":
        if installed:
            env.update(claude_env_declaration())
        env["CLAUDE_CODE_SESSION_ID"] = sid
        env["CLAUDECODE"] = "1"
    elif harness == "codex":
        if installed:
            policy = codex_policy()
            for key in list(env):
                if any(fnmatch.fnmatchcase(key.upper(), pattern.upper()) for pattern in policy["exclude"]):
                    del env[key]
            env.update(policy["set"])
        env["CODEX_THREAD_ID"] = sid
    elif harness == "opencode":
        if installed:
            hooked = opencode_shell_env(sid)
            if hooked is None:
                raise ToolMissing("node is not installed")
            env.update(hooked)
    else:
        raise ValueError(harness)
    return env


def foreign_session_values(env: dict, harness: str) -> dict:
    """Non-empty native session ids in ``env`` that belong to a harness other than ``harness``."""
    found = {}
    for other, keys in _FOREIGN_SESSION_KEYS.items():
        if other == harness:
            continue
        for key in keys:
            if env.get(key):
                found[key] = env[key]
    return found

