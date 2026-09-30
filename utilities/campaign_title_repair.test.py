import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_admission as adm
import artifact_producer as P
import artifact_workflow_group_review as R
import campaign_title_repair as repair


class CampaignTitleRepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        campaign = self.root / "campaigns" / "2026-09-01_demo"
        cycle = campaign / "2026-09-01_demo"
        cycle.mkdir(parents=True)
        (campaign / "campaign.json").write_text(json.dumps({
            "campaign_id": "camp_demo", "title": "Old title", "cycles": ["cyc_demo"],
        }), encoding="utf-8")
        os.chmod(campaign / "campaign.json", 0o644)
        self.manifest = cycle / "manifest.json"
        self.manifest.write_text("  \n" + json.dumps({
            "manifest_id": "man_demo", "manifest_revision_id": "mrev_demo",
            "campaign": {"campaign_id": "camp_demo", "title": "Old title"},
            "cycle": {"cycle_id": "cyc_demo"},
        }, indent=2) + "\n", encoding="utf-8")
        self.roots = Path(self.tmp.name) / "roots.json"
        self.roots.write_text(json.dumps({"roots": [{
            "artifact_root_id": "root_demo", "artifact_root_path": str(self.root),
        }]}), encoding="utf-8")
        self.proposals = Path(self.tmp.name) / "proposals.json"
        self.proposals.write_text(json.dumps({"entries": [{
            "artifact_root_id": "root_demo", "campaign_locator": "2026-09-01_demo",
            "display_title": "읽기 좋은 제목",
        }]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_writes_sidecar_and_preserves_manifest_bytes(self):
        before = self.manifest.read_bytes()
        package = repair.prepare(self.roots, self.proposals)
        self.assertNotEqual(package["entries"][0]["manifest_digests"][0], repair.digest_bytes(before))
        self.assertEqual(package["entries"][0]["manifest_digests"][0], repair.digest_json(json.loads(before)))
        self.assertEqual(package["entries"][0]["manifest_bindings"], [{
            "manifest_revision_id": "mrev_demo",
            "manifest_digest": repair.digest_json(json.loads(before)),
        }])
        result = repair.apply(package)
        self.assertEqual(result["campaigns"], 1)
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "읽기 좋은 제목")
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "campaigns/2026-09-01_demo/campaign.json").st_mode), 0o644)
        declaration = json.loads((self.root / repair.DISPLAY_TITLE_REL).read_text())
        self.assertEqual(declaration["schema"], repair.DECLARATION_SCHEMA)
        self.assertEqual(declaration["entries"][0]["display_title"], "읽기 좋은 제목")
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(repair.verify(package)["status"], "verified")

    def test_partial_apply_and_rollback_preserve_other_declarations(self):
        path = self.root / repair.DISPLAY_TITLE_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        other = {"campaign_id": "camp_other", "campaign_locator": "other", "display_title": "Kept",
                 "manifest_bindings": [], "manifest_revision_ids": [], "manifest_digests": []}
        path.write_text(json.dumps({"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": "root_demo",
                                    "entries": [other], "custom": "preserved"}))
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        result = json.loads(path.read_text())
        self.assertIn(other, result["entries"])
        self.assertEqual(result["custom"], "preserved")
        self.assertEqual(repair.verify(package)["status"], "verified")
        repair.rollback(package)
        self.assertEqual(json.loads(path.read_text())["entries"], [other])

    def test_open_campaign_without_sealed_manifest_can_apply(self):
        self.manifest.unlink()
        package = repair.prepare(self.roots, self.proposals)
        self.assertEqual(package["entries"][0]["manifest_bindings"], [])
        self.assertEqual(repair.apply(package)["campaigns_changed"], 1)
        self.assertEqual(repair.verify(package)["status"], "verified")
        self.assertFalse(self.manifest.exists())

    def test_prepare_refuses_unlisted_campaign(self):
        self.proposals.write_text(json.dumps({"entries": []}), encoding="utf-8")
        with self.assertRaisesRegex(repair.RepairError, "proposal-missing"):
            repair.prepare(self.roots, self.proposals)

    def test_title_quality_rejects_duplicate_forbidden_and_long(self):
        with self.assertRaisesRegex(repair.RepairError, "display-title-duplicate"):
            repair.validate_titles([
                {"campaign_locator": "a", "display_title": "같은 제목"},
                {"campaign_locator": "b", "display_title": "같은 제목"},
            ])
        with self.assertRaisesRegex(repair.RepairError, "display-title-forbidden"):
            repair.validate_titles([{"campaign_locator": "a", "display_title": "legacy project material"}])
        with self.assertRaisesRegex(repair.RepairError, "display-title-too-long"):
            repair.validate_titles([{"campaign_locator": "a", "display_title": "가" * 35}])

    def test_manifest_bindings_sort_pairs_not_independent_arrays(self):
        rows = [
            {"manifest_revision_id": "mrev_z", "manifest_digest": "sha256:00" + "0" * 62},
            {"manifest_revision_id": "mrev_a", "manifest_digest": "sha256:ff" + "f" * 62},
        ]
        bindings, revisions, digests = repair.manifest_binding_fields(rows)
        self.assertEqual([row["manifest_revision_id"] for row in bindings], ["mrev_a", "mrev_z"])
        self.assertEqual(revisions, ["mrev_a", "mrev_z"])
        self.assertEqual(digests, ["sha256:ff" + "f" * 62, "sha256:00" + "0" * 62])
        self.assertNotEqual(sorted(revisions), sorted(digests))

    def test_extra_manifest_is_allowed_but_missing_anchor_is_rejected(self):
        package = repair.prepare(self.roots, self.proposals)
        extra = self.manifest.parent / "later-cycle"
        extra.mkdir()
        (extra / "manifest.json").write_text(json.dumps({
            "manifest_id": "man_later", "manifest_revision_id": "mrev_later",
            "campaign": {"campaign_id": "camp_demo", "title": "Old title"},
            "cycle": {"cycle_id": "cyc_later"},
        }), encoding="utf-8")
        missing = json.loads(json.dumps(package))
        missing["entries"][0]["manifest_bindings"] = [{
            "manifest_revision_id": "mrev_missing",
            "manifest_digest": "sha256:" + "a" * 64,
        }]
        missing["entries"][0]["manifest_revision_ids"] = ["mrev_missing"]
        missing["entries"][0]["manifest_digests"] = ["sha256:" + "a" * 64]
        with self.assertRaisesRegex(repair.RepairError, "manifest-binding-drift"):
            repair.apply(missing)
        repair.apply(package)
        self.assertEqual(repair.verify(package)["status"], "verified")

    def test_nested_payload_manifest_is_not_discovered_as_cycle_control(self):
        payload = self.manifest.parent / "artifacts" / "_internal" / "candidate"
        payload.mkdir(parents=True)
        (payload / "manifest.json").write_text(json.dumps({
            "manifest_id": "man_spoof", "manifest_revision_id": "mrev_spoof",
            "campaign": {"campaign_id": "camp_demo", "title": "Spoof"},
            "cycle": {"cycle_id": "cyc_demo"},
        }), encoding="utf-8")
        rows = repair.manifest_rows(self.manifest.parents[1])
        self.assertEqual([row["manifest_id"] for row in rows], ["man_demo"])

    def test_transaction_failure_restores_pre_apply_bytes_separately_from_full_rollback(self):
        package = repair.prepare(self.roots, self.proposals)
        package["transaction_journal_path"] = str(Path(self.tmp.name) / "failure-journal.json")
        original_write_atomic = repair.write_atomic

        def fail_sidecar(path, value):
            if Path(path).name == "campaign-display-titles.json":
                raise RuntimeError("injected sidecar publish failure")
            return original_write_atomic(path, value)

        with patch.object(repair, "write_atomic", side_effect=fail_sidecar):
            with self.assertRaisesRegex(RuntimeError, "injected sidecar publish failure"):
                repair.apply(package)

        campaign_json = self.root / "campaigns/2026-09-01_demo/campaign.json"
        self.assertEqual(json.loads(campaign_json.read_text())['title'], "Old title")
        self.assertFalse((self.root / repair.DISPLAY_TITLE_REL).exists())
        self.assertEqual(repair.read_json(Path(package["transaction_journal_path"]))["state"], "rolled-back")

    def test_review_and_promote_preserve_current_state_without_writing_campaigns(self):
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        applied = Path(self.tmp.name) / "applied.json"
        package["entries"][0]["original_title"] = "Earliest title"
        applied.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        review_path = Path(self.tmp.name) / "review.json"
        review = repair.review_snapshot(self.roots, self.proposals, applied)
        repair.write_atomic(review_path, review)
        self.assertEqual(review["schema"], repair.REVIEW_SCHEMA)
        self.assertEqual(review["entries"][0]["original_title"], "Earliest title")
        self.assertEqual(review["entries"][0]["current_title"], "읽기 좋은 제목")
        promoted_path = Path(self.tmp.name) / "promoted.json"
        result = repair.promote(review_path, promoted_path, "APPROVE hearting-campaign-title-review/v1")
        self.assertEqual(result["campaigns"], 1)
        promoted = repair.read_json(promoted_path)
        self.assertEqual(promoted["entries"][0]["old_campaign_title"], "읽기 좋은 제목")
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "읽기 좋은 제목")
        promoted["transaction_journal_path"] = str(Path(self.tmp.name) / "journal.json")
        repair.apply(promoted)
        self.assertEqual(repair.read_json(Path(self.tmp.name) / "journal.json")["state"], "committed")
        rollback_result = repair.rollback(promoted)
        self.assertEqual(rollback_result["rollback_kind"], "full-original")
        self.assertEqual(json.loads((self.root / "campaigns/2026-09-01_demo/campaign.json").read_text())["title"], "Earliest title")
        self.assertFalse((self.root / repair.DISPLAY_TITLE_REL).exists())

    def test_review_scopes_to_applied_package_and_records_new_unapproved_campaign(self):
        package = repair.prepare(self.roots, self.proposals)
        repair.apply(package)
        extra = self.root / "campaigns" / "2026-09-13_new_campaign"
        extra.mkdir()
        (extra / "campaign.json").write_text(json.dumps({"campaign_id": "camp_extra", "title": "unapproved"}), encoding="utf-8")
        applied = Path(self.tmp.name) / "applied.json"
        applied.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        review = repair.review_snapshot(self.roots, self.proposals, applied)
        self.assertEqual(len(review["entries"]), 1)
        self.assertEqual(review["unreviewed_campaigns"][0]["campaign_id"], "camp_extra")


_SPEC = importlib.util.spec_from_file_location(
    "campaign_title_repair_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)

PAST = 1_700_000_000.0
START = "=== CAMPAIGN DATA ===\n"
END = "\n=== END DATA ==="


def data_of(prompt):
    return json.loads(prompt.split(START, 1)[1].rsplit(END, 1)[0])


def fake(title, reason="근거 한 문장", harness="claude"):
    """An `invoke` that records prompts and answers with one title; the model is never called."""
    calls = []

    def invoke(prompt):
        calls.append(prompt)
        return json.dumps({"display_title": title, "reason": reason}, ensure_ascii=False), harness
    invoke.calls = calls
    return invoke


def raw(text, harness="claude"):
    return lambda prompt: (text, harness)


def tree_snapshot(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        meta = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
        rows.append((str(path.relative_to(root)), meta.st_size, meta.st_mtime_ns, digest))
    return rows


class AutoBase(fixture.ProducerTestBase):
    def setUp(self):
        self._attempt = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._restore_attempt)
        super().setUp()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(repair.AUTO_DISABLE_ENV, None)
        self.activate()

    def _restore_attempt(self):
        if self._attempt is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = self._attempt

    # -- fixtures -------------------------------------------------------
    def seal(self, key="camp", slug="cycle", now=None):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key, now=now)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork for " + slug.encode() + b"\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md", now=now)
        return begun

    def ready(self, key="camp", slug="cycle"):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork\n")
        self.close(route, route_file)
        return begun

    def finalize(self, begun):
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md")

    def cdir(self, begun):
        for directory in repair.artifact_locator.iter_campaign_dirs(self.root):
            if repair.read_json(directory / "campaign.json").get("campaign_id") == begun["campaign_id"]:
                return directory
        raise AssertionError("campaign directory not found")

    def set_campaign_title(self, cdir, title):
        record = repair.read_json(cdir / "campaign.json")
        record["title"] = title
        repair.write_atomic(cdir / "campaign.json", record)

    def set_manifest_title(self, cdir, title):
        for path in cdir.rglob("manifest.json"):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["campaign"]["title"] = title
            path.write_text(json.dumps(manifest), encoding="utf-8")

    @property
    def declaration_path(self):
        return self.root / repair.DISPLAY_TITLE_REL

    def declaration(self):
        return json.loads(self.declaration_path.read_text(encoding="utf-8"))

    def entry(self, cdir, title):
        record = repair.read_json(cdir / "campaign.json")
        rows = repair.manifest_rows(cdir)
        bindings, revisions, digests = repair.manifest_binding_fields(rows)
        return {"campaign_id": record["campaign_id"], "campaign_locator": cdir.name, "display_title": title,
                "manifest_bindings": bindings, "manifest_revision_ids": revisions, "manifest_digests": digests}

    def write_declaration(self, entries, **top):
        doc = {"schema": repair.DECLARATION_SCHEMA, "artifact_root_id": fixture.ROOT_ID,
               "entries": sorted(entries, key=lambda row: row["campaign_id"]), **top}
        repair.write_atomic(self.declaration_path, doc)
        return doc

    def log_rows(self):
        path = self.root / repair.AUTO_LOG_REL
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def human_apply(self, cdir, title):
        roots = Path(self._tmp.name) / "human-roots.json"
        roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                "artifact_root_path": str(self.root)}]}), encoding="utf-8")
        proposals = Path(self._tmp.name) / "human-proposals.json"
        proposals.write_text(json.dumps({"entries": [{
            "artifact_root_id": fixture.ROOT_ID, "campaign_locator": cdir.name, "display_title": title}]},
            ensure_ascii=False), encoding="utf-8")
        package = repair.prepare(roots, proposals, allow_unlisted=True)
        return package, repair.apply(package)

    def auto(self, begun, invoke, **kw):
        return repair.auto_title(self.root, campaign_ids=[begun["campaign_id"]], mode="seal", invoke=invoke, **kw)


class AutoSelectionAndWriteTest(AutoBase):
    def test_untitled_campaign_seal_writes_korean_title(self):  # A1
        begun = self.seal(key="ax-train", slug="train")
        cdir = self.cdir(begun)
        campaign_before = (cdir / "campaign.json").read_bytes()
        manifests_before = {path: path.read_bytes() for path in cdir.rglob("manifest.json")}
        invoke = fake("AX 명령어 모델 학습")
        result = self.auto(begun, invoke)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["status"] for row in result["targets"]], ["written"])
        self.assertEqual(result["targets"][0]["reason"], "missing")
        document = self.declaration()
        self.assertEqual(document["schema"], repair.DECLARATION_SCHEMA)
        self.assertEqual(document["ruleset"], repair.DEFAULT_RULESET)
        (entry,) = document["entries"]
        self.assertEqual(entry["display_title"], "AX 명령어 모델 학습")
        rows = repair.manifest_rows(cdir)
        self.assertEqual(entry["manifest_bindings"], [{"manifest_revision_id": rows[0]["manifest_revision_id"],
                                                        "manifest_digest": rows[0]["manifest_digest"]}])
        self.assertEqual(entry["manifest_revision_ids"], [rows[0]["manifest_revision_id"]])
        self.assertEqual(entry["manifest_digests"], [rows[0]["manifest_digest"]])
        repair.check_cairn_declaration(document, fixture.ROOT_ID)
        self.assertEqual((cdir / "campaign.json").read_bytes(), campaign_before)
        self.assertEqual({path: path.read_bytes() for path in cdir.rglob("manifest.json")}, manifests_before)
        (line,) = self.log_rows()
        self.assertEqual((line["status"], line["campaign_id"], line["harness"]), ("written", begun["campaign_id"], "claude"))
        data = data_of(invoke.calls[0])
        self.assertEqual(data["campaign"]["current_title"], "ax-train")
        self.assertEqual(len(data["documents"]), 1)

    def test_human_korean_title_is_never_touched(self):  # A2
        with_entry = self.seal(key="human-a", slug="a")
        without_entry = self.seal(key="human-b", slug="b")
        entry_dir, record_dir = self.cdir(with_entry), self.cdir(without_entry)
        self.write_declaration([self.entry(entry_dir, "사람이 정한 한글 제목")])
        self.set_campaign_title(record_dir, "기록에 있는 한글 제목")
        before = self.declaration_path.read_bytes()
        invoke = fake("자동 제목이다")
        result = repair.auto_title(self.root, mode="explicit", invoke=invoke)
        self.assertEqual((invoke.calls, result["targets"], result["protected"]), ([], [], 2))
        self.assertEqual(self.declaration_path.read_bytes(), before)
        self.assertEqual(self.log_rows(), [])

    def test_human_ascii_title_differing_from_key_is_protected(self):  # A3
        begun = self.seal(key="asciiproj", slug="a")
        cdir = self.cdir(begun)
        self.write_declaration([self.entry(cdir, "Human Chosen Name")])
        selection = repair.select_auto_targets(self.root)
        self.assertEqual(selection.targets, [])
        self.assertEqual([row["code"] for row in selection.protected], ["human"])
        invoke = fake("자동 제목이다")
        self.auto(begun, invoke)
        self.assertEqual(invoke.calls, [])

    def test_slug_equal_entry_is_a_target_and_others_stay(self):  # A4
        slug_case = self.seal(key="slug-eq", slug="a")
        first_title_case = self.seal(key="first-title", slug="b")
        kept = self.seal(key="kept-one", slug="c")
        slug_dir, title_dir, kept_dir = self.cdir(slug_case), self.cdir(first_title_case), self.cdir(kept)
        self.set_manifest_title(title_dir, "Original English Title")
        kept_entry = self.entry(kept_dir, "그대로 두는 제목")
        self.write_declaration([self.entry(slug_dir, "slug-eq"), self.entry(title_dir, "Original English Title"), kept_entry],
                               ruleset="convention-x", custom="preserved")
        selection = repair.select_auto_targets(self.root)
        self.assertEqual(sorted((t.locator, t.reason) for t in selection.targets),
                         sorted([(slug_dir.name, "entry-key"), (title_dir.name, "entry-key")]))
        titles = iter(["슬러그 대신 쓰는 제목", "영문 제목 대신 쓰는 제목"])
        result = repair.auto_title(self.root, mode="backfill",
                                   invoke=lambda prompt: (json.dumps({"display_title": next(titles)}, ensure_ascii=False), "codex"))
        self.assertEqual([row["status"] for row in result["targets"]], ["written", "written"])
        document = self.declaration()
        self.assertEqual((document["ruleset"], document["custom"]), ("convention-x", "preserved"))
        by_id = {row["campaign_id"]: row for row in document["entries"]}
        self.assertEqual(by_id[kept["campaign_id"]], kept_entry)
        self.assertEqual([row["campaign_id"] for row in document["entries"]], sorted(by_id))
        self.assertTrue(all(repair.HANGUL_RE.search(by_id[cid]["display_title"])
                            for cid in (slug_case["campaign_id"], first_title_case["campaign_id"])))
        repair.check_cairn_declaration(document, fixture.ROOT_ID)

    def test_model_failure_keeps_seal_and_declaration(self):  # A5
        begun = self.seal(key="fails", slug="a")
        result = self.auto(begun, raw(""))
        self.assertEqual(result["targets"][0]["status"], "failed")
        self.assertEqual(result["targets"][0]["failure_class"], "unavailable")
        self.assertFalse(self.declaration_path.exists())
        (line,) = self.log_rows()
        self.assertEqual((line["status"], line["failure_class"]), ("failed", "unavailable"))
        raising = self.ready(key="hook", slug="hook-raises")
        with patch.object(repair, "launch_after_seal", side_effect=RuntimeError("boom")):
            self.finalize(raising)
        self.assertEqual(P.read_cycle_record(self.root, raising["cycle_id"])["state"], "sealed")
        spawning = self.ready(key="spawn", slug="spawn-fails")
        with patch.object(R, "in_test_process", return_value=False), \
                patch.object(repair.subprocess, "Popen", side_effect=OSError("no fork")) as popen:
            self.finalize(spawning)
        self.assertTrue(any("auto" in call.args[0] for call in popen.call_args_list))
        self.assertEqual(P.read_cycle_record(self.root, spawning["cycle_id"])["state"], "sealed")
        self.assertTrue(list(self.cdir(spawning).rglob("manifest.json")))

    def test_invalid_responses_are_rejected(self):  # A6
        begun = self.seal(key="invalid", slug="a")
        other = self.seal(key="other", slug="b")
        cdir = self.cdir(begun)
        self.set_manifest_title(cdir, "원래 제목 그대로")
        self.set_campaign_title(self.cdir(other), "겹치는 제목")
        cases = {
            "no-korean": "Train Model",
            "too-long": "가" * 35,
            "generic": "요약",
            "key-equal": "원래 제목 그대로",
            "duplicate": "겹치는 제목",
            "keys": '{"display_title": "한글 제목", "extra": 1}',
            "control": '{"display_title": "가나\\n다라"}',
            "parse": "not json at all",
            "empty": '{"display_title": "  "}',
        }
        for code, reply in cases.items():
            with self.subTest(code):
                text = reply if reply.startswith("{") or code == "parse" else json.dumps({"display_title": reply}, ensure_ascii=False)
                result = self.auto(begun, raw(text))
                self.assertEqual((result["targets"][0]["status"], result["targets"][0]["code"]), ("failed", code))
                self.assertEqual(result["targets"][0]["failure_class"], "invalid-response")
                self.assertFalse(self.declaration_path.exists())
                self.assertEqual(self.log_rows()[-1]["code"], code)
        with self.subTest("fenced"):
            fenced = "```json\n" + json.dumps({"display_title": "펜스로 감싼 정상 제목", "reason": "ok"}, ensure_ascii=False) + "\n```\n끝."
            self.assertEqual(self.auto(begun, raw(fenced))["targets"][0]["status"], "written")

    def test_failure_is_retried_at_next_seal(self):  # A7
        begun = self.seal(key="retry", slug="first", now=PAST)
        self.assertEqual(self.auto(begun, raw(""))["targets"][0]["status"], "failed")
        self.seal(key="retry", slug="second", now=PAST + 100)
        result = self.auto(begun, fake("다시 시도해서 얻은 제목"))
        self.assertEqual(result["targets"][0]["status"], "written")
        self.assertEqual([row["status"] for row in self.log_rows()], ["failed", "written"])

    def test_later_sealed_cycle_needs_no_new_entry(self):  # A8
        begun = self.seal(key="later", slug="first", now=PAST)
        self.assertEqual(self.auto(begun, fake("처음 봉인에서 얻은 제목"))["targets"][0]["status"], "written")
        entry_before = self.declaration()["entries"][0]
        self.seal(key="later", slug="second", now=PAST + 100)
        invoke = fake("두 번째 제목이다")
        result = self.auto(begun, invoke)
        self.assertEqual((invoke.calls, result["targets"], result["protected"]), ([], [], 1))
        self.assertEqual(self.declaration()["entries"][0], entry_before)
        cdir = self.cdir(begun)
        self.assertEqual(len(repair.manifest_rows(cdir)), 2)
        self.assertTrue(repair.manifest_bindings_contain(
            repair.manifest_binding_fields(repair.manifest_rows(cdir))[0], repair.entry_manifest_bindings(entry_before)))

    def test_no_sealed_manifest_waits(self):  # A9
        route, route_file = self.route(slug="open", campaign_key="open-camp")
        P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                campaign_key="open-camp")
        invoke = fake("열린 캠페인 제목")
        result = repair.auto_title(self.root, mode="explicit", invoke=invoke)
        self.assertEqual((invoke.calls, result["targets"], result["waiting"]), ([], [], 1))

    def test_unreadable_declaration_is_never_overwritten(self):  # A10
        begun = self.seal(key="unreadable", slug="a")
        for label, payload in (("broken", b"{not json"),
                               ("other-root", json.dumps({"schema": repair.DECLARATION_SCHEMA,
                                                          "artifact_root_id": "root_" + "9" * 32,
                                                          "entries": []}).encode())):
            with self.subTest(label):
                self.declaration_path.write_bytes(payload)
                invoke = fake("덮어쓰면 안 되는 제목")
                result = self.auto(begun, invoke)
                self.assertEqual(result["status"], "declaration-unreadable")
                self.assertEqual(invoke.calls, [])
                self.assertEqual(self.declaration_path.read_bytes(), payload)

    def test_dry_run_writes_nothing(self):  # A11
        begun = self.seal(key="dry", slug="a")
        before = tree_snapshot(self.root)
        result = self.auto(begun, fake("미리 보는 제목이다"), dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["display_title"]),
                         ("proposed", "미리 보는 제목이다"))
        failed = self.auto(begun, raw(""), dry_run=True)
        self.assertEqual(failed["targets"][0]["failure_class"], "unavailable")
        self.assertEqual(tree_snapshot(self.root), before)

    def test_busy_lock_returns_busy(self):  # A14
        begun = self.seal(key="busy", slug="a")
        held = R._try_flock(self.root, self.root / repair.AUTO_LOCK_REL)
        self.assertIsNotNone(held)
        try:
            invoke = fake("바쁠 때 제목")
            result = self.auto(begun, invoke)
        finally:
            R._unlock(held)
        self.assertEqual((result["status"], result["targets"], invoke.calls), ("busy", [], []))
        self.assertEqual(result["queued"], [begun["campaign_id"]])
        self.assertEqual(repair._pending_ids(self.root), [begun["campaign_id"]])
        self.assertEqual(self.auto(begun, fake("이제는 쓰는 제목"))["targets"][0]["status"], "written")
        self.assertEqual(repair._pending_ids(self.root), [])

    def test_campaign_sealed_while_another_run_holds_the_lock_is_titled(self):  # A14b
        first = self.seal(key="race-first", slug="a")
        paused, resume = threading.Event(), threading.Event()
        titles = iter(["첫 번째 캠페인 제목", "두 번째 캠페인 제목"])
        seen = []

        def invoke(prompt):
            seen.append(data_of(prompt)["campaign"]["current_title"])
            if len(seen) == 1:
                paused.set()  # the first run has selected its targets and holds the lock
                self.assertTrue(resume.wait(30))
            return json.dumps({"display_title": next(titles), "reason": "근거 한 문장"}, ensure_ascii=False), "claude"

        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(repair.auto_title(
            self.root, campaign_ids=[first["campaign_id"]], mode="seal", invoke=invoke)))
        worker.start()
        try:
            self.assertTrue(paused.wait(30))
            second = self.seal(key="race-second", slug="b")
            busy = self.auto(second, fake("호출되면 안 되는 제목"))
            self.assertEqual((busy["status"], busy["targets"]), ("busy", []))
            self.assertEqual(repair._pending_ids(self.root), [second["campaign_id"]])
            self.assertIn(("queued", "busy-pending"), [(row["status"], row.get("code")) for row in self.log_rows()])
        finally:
            resume.set()
            worker.join(60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual([row["campaign_id"] for row in outcome["targets"]],
                         [first["campaign_id"], second["campaign_id"]])
        self.assertEqual([row["status"] for row in outcome["targets"]], ["written", "written"])
        titled = {entry["campaign_id"] for entry in self.declaration()["entries"]}
        self.assertEqual(titled, {first["campaign_id"], second["campaign_id"]})
        self.assertEqual(repair._pending_ids(self.root), [])
        self.assertEqual([row["status"] for row in self.log_rows()], ["queued", "written", "written"])
        self.assertEqual(self.log_rows()[2]["mode"], "pending")

    def test_pending_marker_for_an_already_titled_campaign_is_cleared(self):
        begun = self.seal(key="stale", slug="a")
        self.assertEqual(self.auto(begun, fake("이미 제목이 있는 캠페인"))["targets"][0]["status"], "written")
        repair._touch_pending(self.root, begun["campaign_id"])
        repair._touch_pending(self.root, "not-a-campaign")
        self.assertEqual(repair._pending_ids(self.root), [begun["campaign_id"]])
        invoke = fake("다시 쓰면 안 되는 제목")
        repair.auto_title(self.root, campaign_ids=[begun["campaign_id"]], mode="seal", invoke=invoke)
        self.assertEqual((invoke.calls, repair._pending_ids(self.root)), ([], []))



class AutoTriggerAndCliTest(AutoBase):
    def spawned(self):
        return patch.object(repair.subprocess, "Popen")

    def test_trigger_rules(self):  # A12
        begun = self.seal(key="trigger", slug="a")
        record = {"campaign_id": begun["campaign_id"]}
        with self.spawned() as popen:
            self.assertFalse(repair.launch_after_seal(self.root, record))  # test process
        popen.assert_not_called()
        with patch.object(R, "in_test_process", return_value=False):
            with self.spawned() as popen:
                self.assertTrue(repair.launch_after_seal(self.root, record))
            popen.assert_called_once()
            argv = popen.call_args.args[0]
            self.assertEqual(argv[1], str(Path(repair.__file__).resolve()))
            self.assertEqual(argv[2:], ["auto", "--artifact-root", str(self.root), "--campaign",
                                        begun["campaign_id"], "--mode", "seal"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            with self.spawned() as popen, patch.dict(os.environ, {repair.AUTO_DISABLE_ENV: "off"}):
                self.assertFalse(repair.launch_after_seal(self.root, record))
            with self.spawned() as popen:
                self.assertFalse(repair.launch_after_seal(self.root / "nowhere", record))
                self.assertFalse(repair.launch_after_seal(self.root, {"campaign_id": "not-a-campaign"}))
                self.assertFalse(repair.launch_after_seal(self.root, {}))
            popen.assert_not_called()

    def test_backfill_over_root_declaration(self):  # A13
        begun = self.seal(key="backfill", slug="a")
        copy = Path(self._tmp.name) / "copy-root"
        shutil.copytree(self.root, copy)
        roots = Path(self._tmp.name) / "roots.json"
        roots.write_text(json.dumps({"roots": [
            {"artifact_root_id": fixture.ROOT_ID, "artifact_root_path": str(self.root), "display_name": "main"},
            {"artifact_root_id": "root_" + "c" * 32, "artifact_root_path": str(copy)},
            {"artifact_root_id": "root_" + "d" * 32, "artifact_root_path": str(copy / "missing")},
        ]}), encoding="utf-8")
        result = repair.backfill_roots(roots, invoke=fake("소급 적용한 제목"))
        first, mismatch, missing = result["roots"]
        self.assertEqual((first["status"], first["targets"], first["written"], first["display_name"]), ("ok", 1, 1, "main"))
        self.assertEqual([(row["status"], row["code"]) for row in (mismatch, missing)],
                         [("skipped", "root-identity-mismatch")] * 2)
        self.assertEqual((result["totals"]["targets"], result["totals"]["written"], result["totals"]["failed"]), (1, 1, 0))
        self.assertFalse((copy / repair.DISPLAY_TITLE_REL).exists())
        self.assertEqual(self.log_rows()[0]["campaign_id"], begun["campaign_id"])

    def test_cli_auto_and_backfill(self):  # A15
        begun = self.seal(key="cli", slug="a")

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = repair.main(list(argv))
            lines = out.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            return code, json.loads(lines[0])

        with patch.object(repair, "_default_invoke", return_value=(json.dumps({"display_title": "명령줄로 만든 제목"}, ensure_ascii=False), "claude")):
            code, result = run("auto", "--artifact-root", str(self.root), "--campaign", begun["campaign_id"], "--dry-run")
            self.assertEqual((code, result["status"], result["targets"][0]["status"]), (0, "dry-run", "proposed"))
            self.assertFalse(self.declaration_path.exists())
            roots = Path(self._tmp.name) / "cli-roots.json"
            roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                    "artifact_root_path": str(self.root)}]}), encoding="utf-8")
            code, result = run("backfill", "--root-declaration", str(roots), "--dry-run")
            self.assertEqual((code, result["status"], result["totals"]["proposed"]), (0, "dry-run", 1))
            code, result = run("auto", "--artifact-root", str(self.root), "--mode", "explicit")
            self.assertEqual((code, result["targets"][0]["status"]), (0, "written"))
        with patch.object(repair, "_default_invoke", return_value=("", None)):
            other = self.seal(key="cli-fail", slug="b")
            code, result = run("auto", "--artifact-root", str(self.root), "--campaign", other["campaign_id"])
            self.assertEqual((code, result["targets"][0]["failure_class"]), (0, "unavailable"))
        self.assertEqual(run("auto", "--artifact-root", str(self.root / "nowhere"))[0], 65)
        self.assertEqual(run("auto", "--artifact-root", str(self.root), "--campaign", "bad")[1]["code"], "campaign-id-invalid")
        self.assertEqual(run("auto", "--artifact-root", str(self.root), "--limit", "0")[0], 65)
        self.assertEqual(run("backfill", "--root-declaration", str(self.root / "missing.json"))[0], 65)


class HumanRaceAndContractTest(AutoBase):
    def test_human_apply_between_select_and_write_wins(self):  # A16
        begun = self.seal(key="race-a", slug="a")
        cdir = self.cdir(begun)

        def invoke(prompt):
            self.human_apply(cdir, "사람이 그사이 정한 제목")
            return json.dumps({"display_title": "자동으로 만든 제목"}, ensure_ascii=False), "claude"

        result = self.auto(begun, invoke)
        self.assertEqual((result["targets"][0]["status"], result["targets"][0]["code"]), ("skipped", "raced-human"))
        (entry,) = self.declaration()["entries"]
        self.assertEqual(entry["display_title"], "사람이 그사이 정한 제목")
        self.assertEqual(repair.read_json(cdir / "campaign.json")["title"], "사람이 그사이 정한 제목")
        (line,) = self.log_rows()
        self.assertEqual((line["status"], line["code"]), ("skipped", "raced-human"))

    def test_human_apply_after_auto_write_wins(self):  # A17
        first = self.seal(key="race-b", slug="a", now=PAST)
        second = self.seal(key="race-c", slug="b", now=PAST + 10)
        self.assertEqual(self.auto(first, fake("자동으로 먼저 쓴 제목"))["targets"][0]["status"], "written")
        self.assertEqual(self.auto(second, fake("다른 캠페인 자동 제목"))["targets"][0]["status"], "written")
        other_entry = next(row for row in self.declaration()["entries"] if row["campaign_id"] == second["campaign_id"])
        package, _ = self.human_apply(self.cdir(first), "사람이 나중에 정한 제목")
        by_id = {row["campaign_id"]: row for row in self.declaration()["entries"]}
        self.assertEqual(by_id[first["campaign_id"]]["display_title"], "사람이 나중에 정한 제목")
        self.assertEqual(by_id[second["campaign_id"]], other_entry)
        self.seal(key="race-b", slug="c", now=PAST + 20)
        invoke = fake("무시되어야 하는 제목")
        result = self.auto(first, invoke)
        self.assertEqual((invoke.calls, result["targets"]), ([], []))

    def test_busy_declaration_lock_fails_auto_and_restores_apply(self):  # A17
        begun = self.seal(key="race-d", slug="a")
        cdir = self.cdir(begun)
        selection = repair.select_auto_targets(self.root)
        (target,) = selection.targets
        roots = Path(self._tmp.name) / "busy-roots.json"
        roots.write_text(json.dumps({"roots": [{"artifact_root_id": fixture.ROOT_ID,
                                                "artifact_root_path": str(self.root)}]}), encoding="utf-8")
        proposals = Path(self._tmp.name) / "busy-proposals.json"
        proposals.write_text(json.dumps({"entries": [{"artifact_root_id": fixture.ROOT_ID,
                                                       "campaign_locator": cdir.name, "display_title": "사람 제목이다"}]},
                                        ensure_ascii=False), encoding="utf-8")
        package = repair.prepare(roots, proposals, allow_unlisted=True)
        package["transaction_journal_path"] = str(Path(self._tmp.name) / "journal.json")
        campaign_before = (cdir / "campaign.json").read_bytes()
        with repair._declaration_lock(self.root):
            with self.assertRaises(repair.AutoTitleError) as auto_error:
                repair.write_auto_entry(self.root, target, "잠겨서 못 쓰는 제목", lock_timeout=0.05)
            self.assertEqual((auto_error.exception.failure_class, auto_error.exception.code), ("write-failed", "busy"))
            with patch.object(repair, "REPAIR_LOCK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(repair.RepairError, "declaration-lock-busy"):
                    repair.apply(package)
        self.assertEqual((cdir / "campaign.json").read_bytes(), campaign_before)
        self.assertFalse(self.declaration_path.exists())
        self.assertEqual(repair.read_json(Path(package["transaction_journal_path"]))["state"], "rolled-back")
        self.assertEqual(repair.apply(package)["status"], "applied")
        applied_declaration = self.declaration_path.read_bytes()
        with repair._declaration_lock(self.root), patch.object(repair, "REPAIR_LOCK_TIMEOUT", 0.05):
            with self.assertRaisesRegex(repair.RepairError, "declaration-lock-busy"):
                repair.rollback(package)
        self.assertEqual(self.declaration_path.read_bytes(), applied_declaration)
        self.assertEqual(repair.rollback(package)["status"], "rolled-back")

    def test_written_declaration_passes_cairn_contract(self):  # A18
        single = self.seal(key="c-single", slug="a", now=PAST)
        multi = self.seal(key="c-multi", slug="b", now=PAST + 10)
        self.seal(key="c-multi", slug="c", now=PAST + 20)
        target = self.seal(key="c-target", slug="d", now=PAST + 30)
        single_entry, multi_entry = self.entry(self.cdir(single), "바인딩 하나 제목"), self.entry(self.cdir(multi), "바인딩 여럿 제목")
        self.assertEqual((len(single_entry["manifest_bindings"]), len(multi_entry["manifest_bindings"])), (1, 2))
        for entry in (single_entry, multi_entry):
            self.assertTrue(all(re.fullmatch(r"mrev_[0-9a-f]{32}", rev) for rev in entry["manifest_revision_ids"]))
        self.write_declaration([single_entry, multi_entry])
        self.assertEqual(self.auto(target, fake("계약을 통과하는 제목"))["targets"][0]["status"], "written")
        document = self.declaration()
        repair.check_cairn_declaration(document, fixture.ROOT_ID)
        self.assertEqual(len(document["entries"]), 3)

        def broken(mutate):
            copy = json.loads(json.dumps(document))
            mutate(next(row for row in copy["entries"] if row["campaign_id"] == multi["campaign_id"]))
            return copy

        negatives = {
            "bindings-empty": lambda e: e.update(manifest_bindings=[], manifest_revision_ids=[], manifest_digests=[]),
            "bindings-invalid": lambda e: e.update(manifest_bindings=e["manifest_bindings"][::-1],
                                                   manifest_revision_ids=e["manifest_revision_ids"][::-1],
                                                   manifest_digests=e["manifest_digests"][::-1]),
            "bindings-duplicate": lambda e: (e["manifest_bindings"][1].update(manifest_digest=e["manifest_bindings"][0]["manifest_digest"]),
                                             e.update(manifest_digests=[e["manifest_bindings"][0]["manifest_digest"]] * 2)),
            "binding-arrays-length": lambda e: e.update(manifest_digests=e["manifest_digests"][:1]),
            "binding-arrays-mismatch": lambda e: e.update(manifest_revision_ids=e["manifest_revision_ids"][::-1]),
            "campaign-id-duplicate": lambda e: e.update(campaign_id=single["campaign_id"]),
            "campaign-id-or-title-empty": lambda e: e.update(display_title="  "),
        }
        for code, mutate in negatives.items():
            with self.subTest(code):
                with self.assertRaises(repair.AutoTitleError) as ctx:
                    repair.check_cairn_declaration(broken(mutate), fixture.ROOT_ID)
                self.assertEqual(ctx.exception.code, f"cairn-contract:{code}")
        with self.subTest("digest-format"):
            def bad_digest(e):
                e["manifest_bindings"][0]["manifest_digest"] = "md5:abc"
            with self.assertRaises(repair.AutoTitleError) as ctx:
                repair.check_cairn_declaration(broken(bad_digest), fixture.ROOT_ID)
            self.assertEqual(ctx.exception.code, "cairn-contract:bindings-invalid")
        with self.subTest("existing-row-already-wrong"):
            wrong = json.loads(json.dumps(document))
            wrong["entries"] = [row for row in wrong["entries"] if row["campaign_id"] != target["campaign_id"]]
            wrong["entries"][0].update(manifest_bindings=[], manifest_revision_ids=[], manifest_digests=[])
            self.declaration_path.write_text(json.dumps(wrong), encoding="utf-8")
            before = self.declaration_path.read_bytes()
            other = self.seal(key="c-other", slug="e", now=PAST + 40)
            result = self.auto(other, fake("기존 행이 틀려서 못 쓰는 제목"))
            self.assertEqual((result["targets"][0]["status"], result["targets"][0]["failure_class"]), ("failed", "write-failed"))
            self.assertTrue(result["targets"][0]["code"].startswith("cairn-contract:"))
            self.assertEqual(self.declaration_path.read_bytes(), before)

    def test_anchor_change_before_write_skips(self):  # A19
        for label in ("deleted", "changed"):
            with self.subTest(label):
                key = f"anchor-{label}"
                first = self.seal(key=key, slug="first", now=PAST)
                self.seal(key=key, slug="second", now=PAST + 100)
                cdir = self.cdir(first)
                anchor = min(cdir.rglob("manifest.json"), key=lambda path: json.loads(path.read_text())["cycle"]["cycle_id"] != first["cycle_id"])
                self.assertEqual(json.loads(anchor.read_text())["cycle"]["cycle_id"], first["cycle_id"])

                def invoke(prompt, anchor=anchor, label=label):
                    if label == "deleted":
                        anchor.unlink()
                    else:
                        manifest = json.loads(anchor.read_text())
                        manifest["note"] = "changed after selection"
                        anchor.write_text(json.dumps(manifest), encoding="utf-8")
                    return json.dumps({"display_title": "앵커가 바뀐 제목"}, ensure_ascii=False), "claude"

                before = self.declaration_path.read_bytes() if self.declaration_path.exists() else None
                result = self.auto(first, invoke)
                self.assertEqual((result["targets"][0]["status"], result["targets"][0]["code"]), ("skipped", "anchor-changed"))
                self.assertEqual(self.declaration_path.read_bytes() if self.declaration_path.exists() else None, before)
                self.assertEqual(self.log_rows()[-1]["code"], "anchor-changed")
                retry = self.auto(first, fake("새 앵커로 쓰는 제목 " + ("가" if label == "deleted" else "나")))
                self.assertEqual(retry["targets"][0]["status"], "written", retry)
                rows = repair.manifest_rows(cdir)
                entry = next(row for row in self.declaration()["entries"] if row["campaign_id"] == first["campaign_id"])
                self.assertTrue(repair.manifest_bindings_contain(rows, repair.entry_manifest_bindings(entry)))
                repair.check_cairn_declaration(self.declaration(), fixture.ROOT_ID)


if __name__ == "__main__":
    unittest.main()
