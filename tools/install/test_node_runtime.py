#!/usr/bin/env python3
"""Regression tests for the install-time Node.js ensure step.

Policy under test (2026-08-21 user decision, Cairn Node dependency policy):
reuse a compatible Node >= 20.9.0 from PATH; otherwise install the latest
verified LTS into user space; every failure is a warning and never blocks
the install. All network and process surfaces are mocked.
"""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import node_runtime  # noqa: E402


def _fake_archive(top_level: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as archive:
        payload = b"#!/bin/sh\necho v24.19.0\n"
        info = tarfile.TarInfo(f"{top_level}/bin/node")
        info.size = len(payload)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class _HermeticBinDirCase(unittest.TestCase):
    """Every case points ``HARNESS_BIN_DIR`` at a private directory.

    ``ensure_node`` sweeps the launcher dir before any policy decision, so a
    case that left the variable unset would reach the developer's real
    ``~/.local/bin`` -- the very leak this module now repairs.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bin_dir = self.tmp / "bin"
        env = mock.patch.dict(
            node_runtime.os.environ, {"HARNESS_BIN_DIR": str(self.bin_dir)}
        )
        env.start()
        self.addCleanup(env.stop)

    def managed_link(self, name: str, data_root: Path | None = None) -> Path:
        """``<bin>/<name>`` -> ``<data_root>/hearting/node/current/bin/<name>``."""
        root = data_root if data_root is not None else self.tmp / "gone-data"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        link = self.bin_dir / name
        link.symlink_to(root / "hearting" / "node" / "current" / "bin" / name)
        return link

    def reuse(self) -> dict:
        """``ensure_node`` with a compatible node on PATH and no network."""
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                node_runtime.shutil, "which", return_value="/usr/bin/node",
            ))
            stack.enter_context(mock.patch.object(
                node_runtime.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="v20.9.0\n", stderr=""),
            ))
            stack.enter_context(mock.patch.object(node_runtime, "_fetch_bytes"))
            return node_runtime.ensure_node()


class ReuseTest(_HermeticBinDirCase):
    def test_compatible_node_on_path_is_reused_without_network(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                node_runtime.shutil, "which", return_value="/usr/bin/node",
            ))
            stack.enter_context(mock.patch.object(
                node_runtime.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="v20.9.0\n", stderr=""),
            ))
            fetch = stack.enter_context(mock.patch.object(node_runtime, "_fetch_bytes"))
            result = node_runtime.ensure_node()
        self.assertEqual(result["status"], "ok")
        self.assertIn("reusing node v20.9.0", result["detail"])
        fetch.assert_not_called()

    def test_opt_out_env_skips_everything(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                node_runtime.os.environ, {"HARNESS_NO_NODE_INSTALL": "1"},
            ))
            which = stack.enter_context(mock.patch.object(node_runtime.shutil, "which"))
            result = node_runtime.ensure_node()
        self.assertEqual(result["status"], "ok")
        self.assertIn("HARNESS_NO_NODE_INSTALL", result["detail"])
        which.assert_not_called()


class DanglingLinkTest(_HermeticBinDirCase):
    """Dangling links in the managed layout are ours to remove; nothing else is."""

    def test_dangling_managed_links_are_removed_when_node_is_reused(self):
        links = [self.managed_link(name) for name in ("node", "npm", "npx")]
        result = self.reuse()
        self.assertEqual(result["status"], "ok")
        self.assertIn("reusing node v20.9.0", result["detail"])
        self.assertIn(
            f"removed dangling managed node/npm/npx from {self.bin_dir}", result["detail"]
        )
        for link in links:
            self.assertFalse(link.is_symlink(), link)

    def test_dangling_link_outside_the_managed_layout_is_kept(self):
        self.bin_dir.mkdir(parents=True)
        foreign = self.bin_dir / "node"
        foreign.symlink_to(self.tmp / "elsewhere" / "bin" / "node")
        result = self.reuse()
        self.assertNotIn("removed", result["detail"])
        self.assertTrue(foreign.is_symlink())
        self.assertEqual(node_runtime.dangling_links(), [])

    def test_live_managed_link_from_another_data_root_is_kept(self):
        other = self.tmp / "other-data"
        (other / "hearting" / "node" / "current" / "bin").mkdir(parents=True)
        (other / "hearting" / "node" / "current" / "bin" / "node").write_text(
            "#!/bin/sh\n", encoding="utf-8"
        )
        link = self.managed_link("node", data_root=other)
        result = self.reuse()
        self.assertNotIn("removed", result["detail"])
        self.assertTrue(link.is_symlink())
        self.assertTrue(link.exists())

    def test_regular_file_under_a_managed_name_is_kept(self):
        self.bin_dir.mkdir(parents=True)
        (self.bin_dir / "npm").write_text("user-owned\n", encoding="utf-8")
        result = self.reuse()
        self.assertNotIn("removed", result["detail"])
        self.assertEqual((self.bin_dir / "npm").read_text(encoding="utf-8"), "user-owned\n")

    def test_opt_out_still_removes_dangling_managed_links(self):
        link = self.managed_link("node")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                node_runtime.os.environ, {"HARNESS_NO_NODE_INSTALL": "1"},
            ))
            which = stack.enter_context(mock.patch.object(node_runtime.shutil, "which"))
            result = node_runtime.ensure_node()
        which.assert_not_called()
        self.assertIn("HARNESS_NO_NODE_INSTALL", result["detail"])
        self.assertIn("removed dangling managed node", result["detail"])
        self.assertFalse(link.is_symlink())

    def test_status_is_read_only_and_reconcile_repairs_once(self):
        self.assertEqual(node_runtime.launcher_status()["status"], "ok")
        link = self.managed_link("npx")
        status = node_runtime.launcher_status()
        self.assertEqual(status["id"], "host.node-launchers")
        self.assertEqual(status["status"], "dangling")
        self.assertIn(f"dangling managed npx in {self.bin_dir}", status["detail"])
        self.assertTrue(link.is_symlink(), "launcher_status must not mutate")
        repaired = node_runtime.reconcile_launchers()
        self.assertEqual(repaired["status"], "repaired")
        self.assertIn(f"removed dangling managed npx from {self.bin_dir}", repaired["detail"])
        self.assertFalse(link.is_symlink())
        self.assertEqual(node_runtime.reconcile_launchers()["status"], "ok")
        self.assertEqual(node_runtime.launcher_status()["status"], "ok")

    def test_missing_bin_dir_is_sound_and_stays_absent(self):
        self.assertEqual(node_runtime.dangling_links(), [])
        self.assertEqual(node_runtime.reconcile_launchers()["status"], "ok")
        self.assertFalse(self.bin_dir.exists())


class InstallTest(_HermeticBinDirCase):
    def _run_install(self, stack, tmp, *, checksum_ok=True):
        version = "v24.19.0"
        top_level = f"node-{version}-linux-x64"
        archive_bytes = _fake_archive(top_level)
        import hashlib
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if not checksum_ok:
            digest = "0" * 64
        index = json.dumps([{"version": version, "lts": "Jod"}]).encode()
        shasums = f"{digest}  {top_level}.tar.xz\n".encode()

        def fetch(url, limit):
            if url.endswith("index.json"):
                return index
            if url.endswith("SHASUMS256.txt"):
                return shasums
            raise AssertionError(url)

        def download(url, destination, expected):
            import hashlib as h
            actual = h.sha256(archive_bytes).hexdigest()
            if actual != expected:
                raise OSError(f"checksum mismatch for {url}")
            destination.write_bytes(archive_bytes)

        real_run = subprocess.run

        def run(cmd, **kwargs):
            if cmd[:2] == ["tar", "-xJf"]:
                return real_run(cmd, **kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="v24.19.0\n", stderr="")

        stack.enter_context(mock.patch.dict(node_runtime.os.environ, {
            "XDG_DATA_HOME": str(tmp / "data"),
            "HARNESS_BIN_DIR": str(tmp / "bin"),
        }))
        stack.enter_context(mock.patch.object(
            node_runtime.shutil, "which", return_value=None,
        ))
        stack.enter_context(mock.patch.object(node_runtime, "_fetch_bytes", side_effect=fetch))
        stack.enter_context(mock.patch.object(
            node_runtime, "_download_verified", side_effect=download,
        ))
        stack.enter_context(mock.patch.object(
            node_runtime.platform, "system", return_value="Linux",
        ))
        stack.enter_context(mock.patch.object(
            node_runtime.platform, "machine", return_value="x86_64",
        ))
        stack.enter_context(mock.patch.object(node_runtime.subprocess, "run", side_effect=run))
        return node_runtime.ensure_node()

    def test_absent_node_installs_verified_lts_and_exposes_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            with ExitStack() as stack:
                result = self._run_install(stack, tmp)
            self.assertEqual(result["status"], "installed", result["detail"])
            root = tmp / "data" / "hearting" / "node"
            self.assertTrue((root / "v24.19.0" / "bin" / "node").is_file())
            link = tmp / "bin" / "node"
            self.assertTrue(link.is_symlink())
            self.assertEqual(
                link.resolve(), (root / "v24.19.0" / "bin" / "node").resolve()
            )

    def test_checksum_mismatch_degrades_to_warning_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            with ExitStack() as stack:
                result = self._run_install(stack, tmp, checksum_ok=False)
            self.assertEqual(result["status"], "warning")
            self.assertIn("checksum mismatch", result["detail"])
            self.assertFalse((tmp / "bin" / "node").exists())

    def test_dangling_managed_link_is_replaced_when_installing(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "bin").mkdir(parents=True)
            stale = tmp / "bin" / "node"
            stale.symlink_to(
                tmp / "old-data" / "hearting" / "node" / "current" / "bin" / "node"
            )
            with ExitStack() as stack:
                result = self._run_install(stack, tmp)
            self.assertEqual(result["status"], "installed", result["detail"])
            self.assertNotIn("kept foreign", result["detail"])
            self.assertIn("removed dangling managed node", result["detail"])
            root = tmp / "data" / "hearting" / "node"
            self.assertEqual(
                stale.resolve(), (root / "v24.19.0" / "bin" / "node").resolve()
            )

    def test_foreign_bin_entry_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "bin").mkdir(parents=True)
            foreign = tmp / "bin" / "node"
            foreign.write_text("user-owned\n", encoding="utf-8")
            with ExitStack() as stack:
                result = self._run_install(stack, tmp)
            self.assertEqual(result["status"], "installed")
            self.assertIn("kept foreign", result["detail"])
            self.assertFalse(foreign.is_symlink())
            self.assertEqual(foreign.read_text(encoding="utf-8"), "user-owned\n")

    def test_unsupported_platform_is_warning_without_network(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                node_runtime.shutil, "which", return_value=None,
            ))
            stack.enter_context(mock.patch.object(
                node_runtime.platform, "system", return_value="Darwin",
            ))
            fetch = stack.enter_context(mock.patch.object(node_runtime, "_fetch_bytes"))
            result = node_runtime.ensure_node()
        self.assertEqual(result["status"], "warning")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
