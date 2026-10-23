import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import review_state
import run_state
from manifest import create_manifest


class IndependentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "input.md").write_text("The dose is 10 mg.", encoding="utf-8")
        (self.dir / "chunk0001.md").write_text("The dose is 10 mg.", encoding="utf-8")
        (self.dir / "output_chunk0001.md").write_text("剂量是 100 mg。", encoding="utf-8")
        create_manifest(str(self.dir), ["chunk0001.md"], str(self.dir / "input.md"))
        run_state.record_chunks(str(self.dir), ["chunk0001"])

    def report(self, verdict, issues, verification=False):
        path = self.dir / ("verify_chunk0001.json" if verification else "review_chunk0001.json")
        path.write_text(json.dumps({"version": 1, "verdict": verdict, "issues": issues}, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def error():
        return {"category": "numbers", "severity": "major",
                "source_quote": "10 mg", "translation_quote": "100 mg",
                "explanation": "The amount was incorrectly increased tenfold.",
                "suggested_fix": "Change 100 mg to 10 mg."}

    def action(self):
        return review_state.plan(str(self.dir))["chunks"][0]["action"]

    def test_approval_then_translation_edit_invalidates_review(self):
        self.assertEqual(self.action(), "review")
        self.report("pass", [])
        review_state.record(str(self.dir), "chunk0001")
        self.assertTrue(review_state.assert_ready(str(self.dir))["ready"])
        (self.dir / "output_chunk0001.md").write_text("剂量是 10 mg。", encoding="utf-8")
        self.assertEqual(self.action(), "review")
        with self.assertRaises(ValueError):
            review_state.assert_ready(str(self.dir))

    def test_issue_requires_revision_and_independent_verification(self):
        self.report("revise", [self.error()])
        review_state.record(str(self.dir), "chunk0001")
        self.assertEqual(self.action(), "revise")
        with self.assertRaises(ValueError):
            review_state.assert_ready(str(self.dir))
        (self.dir / "output_chunk0001.md").write_text("剂量是 10 mg。", encoding="utf-8")
        self.assertEqual(self.action(), "verify")
        self.report("pass", [], verification=True)
        review_state.record(str(self.dir), "chunk0001", verification=True)
        self.assertEqual(self.action(), "approved")
        self.assertTrue(review_state.assert_ready(str(self.dir))["ready"])
        (self.dir / "verify_chunk0001.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.action(), "review")

    def test_unresolved_verification_blocks_instead_of_looping(self):
        self.report("revise", [self.error()])
        review_state.record(str(self.dir), "chunk0001")
        (self.dir / "output_chunk0001.md").write_text("剂量仍然是 100 毫克。", encoding="utf-8")
        self.report("revise", [{
            **self.error(), "translation_quote": "100 毫克",
            "explanation": "The revision still states 100 rather than 10.",
        }], verification=True)
        review_state.record(str(self.dir), "chunk0001", verification=True)
        self.assertEqual(self.action(), "blocked")

    def test_unverified_report_or_false_quote_rejected(self):
        with self.assertRaises(ValueError):
            review_state.record(str(self.dir), "chunk0001")
        self.report("revise", [{**self.error(), "source_quote": "fabricated text"}])
        with self.assertRaises(ValueError):
            review_state.record(str(self.dir), "chunk0001")
        self.assertEqual(self.action(), "review")

    def test_glossary_change_requires_retranslation_before_review(self):
        self.report("pass", [])
        review_state.record(str(self.dir), "chunk0001")
        (self.dir / "chunk0001.md").write_text("The dose is 20 mg.", encoding="utf-8")
        self.assertEqual(self.action(), "waiting_for_translation")
        with self.assertRaises(ValueError):
            review_state.assert_ready(str(self.dir))

    def test_report_tampering_invalidates_approval(self):
        self.report("pass", [])
        review_state.record(str(self.dir), "chunk0001")
        self.report("revise", [self.error()])
        self.assertEqual(self.action(), "review")

    def test_report_requires_source_evidence_and_actionable_findings(self):
        self.report("pass", [self.error()])
        with self.assertRaises(ValueError):
            review_state.record(str(self.dir), "chunk0001")
        self.report("revise", [])
        with self.assertRaises(ValueError):
            review_state.record(str(self.dir), "chunk0001")
        self.report("revise", [{**self.error(), "translation_quote": "not in translation"}])
        with self.assertRaises(ValueError):
            review_state.record(str(self.dir), "chunk0001")


if __name__ == "__main__":
    unittest.main()
