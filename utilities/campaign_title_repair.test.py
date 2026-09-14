import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
