"""Structured collaborative-signal extraction (shortcoming #4).

Covers: structured JSON fence, inline [SIGNAL ...] markers, legacy free-form
fallback, target-role validation, and prose false-positive guards.
"""
import sys
import unittest

# Isolate standalone runs; no effect when another suite module imported main first.
if "main" not in sys.modules:
    import os
    import tempfile
    os.environ["AI_TEAM_DB"] = os.path.join(tempfile.gettempdir(), "test_ai_team_signals.db")

import main  # noqa: E402

ROLES = ["Orchestrator", "Architect", "Coder", "QA"]


class TestStructuredSignals(unittest.TestCase):
    def test_json_fence_parses_and_normalizes_role(self):
        out = 'review\n```signals\n[{"type": "QUESTION", "to": "coder", "text": "pakai auth apa?"}]\n```'
        sigs = main.extract_stage_signals(out, "Architect", ROLES)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["type"], "QUESTION")
        self.assertEqual(sigs[0]["to"], "Coder")  # role casing normalized
        self.assertEqual(sigs[0]["text"], "pakai auth apa?")

    def test_inline_marker(self):
        sigs = main.extract_stage_signals("[SIGNAL type=BLOCKED to=QA] build gagal", "Coder", ROLES)
        self.assertEqual(sigs, [{"type": "BLOCKED", "to": "QA", "text": "build gagal"}])

    def test_unknown_target_broadcasts_to_all(self):
        sigs = main.extract_stage_signals("[SIGNAL type=FINDING to=Ghost] ada bug", "Coder", ROLES)
        self.assertEqual(sigs[0]["to"], "all")

    def test_invalid_type_dropped(self):
        sigs = main.extract_stage_signals("[SIGNAL type=LOREM to=QA] apalah", "Coder", ROLES)
        self.assertEqual(sigs, [])

    def test_legacy_freeform_fallback(self):
        out = "Hasil review:\nFINDING: bagian auth kurang validasi.\nQUESTION ke QA: sudah cover edge case?\n"
        sigs = main.extract_stage_signals(out, "Architect", ROLES)
        kinds = {s["type"] for s in sigs}
        self.assertEqual(kinds, {"FINDING", "QUESTION"})
        qa = next(s for s in sigs if s["type"] == "QUESTION")
        self.assertEqual(qa["to"], "QA")

    def test_architecture_concern_defaults_to_architect(self):
        sigs = main.extract_stage_signals("catatan:\nARCHITECTURE_CONCERN: layer service bocor", "Coder", ROLES)
        self.assertEqual(sigs[0]["type"], "ARCHITECTURE_CONCERN")
        self.assertEqual(sigs[0]["to"], "Architect")

    def test_prose_does_not_trigger(self):
        self.assertEqual(main.extract_stage_signals("Banyak questions di dokumen ini ya.", "Coder", ROLES), [])

    def test_structured_suppresses_legacy_duplicates(self):
        out = '```signals\n[{"type": "FINDING", "to": "all", "text": "satu"}]\n```\nFINDING: dua'
        sigs = main.extract_stage_signals(out, "Coder", ROLES)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["text"], "satu")


if __name__ == "__main__":
    unittest.main()
