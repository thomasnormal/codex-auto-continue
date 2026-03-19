import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_loop  # noqa: E402


class AcwLoopUnitTests(unittest.TestCase):
    def test_loop_observation_detects_docs_only_checkpoint_block(self):
        text = (
            "• Updated:\n"
            "  - docs/DDR3_PLAN.md\n"
            "  - docs/BMC_LEC_ENGINEERING_LOG.md\n"
            "\n"
            "  Committed:\n"
            "  [DDR3] checkpoint\n"
            "\n"
            "› continue by docs/DDR3_PLAN.md\n"
        )

        observation = acw_loop.loop_observation_from_pane_text(
            text,
            "continue by docs/DDR3_PLAN.md",
        )

        self.assertEqual(
            "docs-only:docs/BMC_LEC_ENGINEERING_LOG.md|docs/DDR3_PLAN.md",
            observation.signature,
        )
        self.assertEqual("docs/BMC_LEC_ENGINEERING_LOG.md, docs/DDR3_PLAN.md", observation.detail)

    def test_loop_observation_rejects_non_doc_checkpoint_block(self):
        text = (
            "• Updated:\n"
            "  - docs/DDR3_PLAN.md\n"
            "  - lib/Conversion/Mem2Reg.cpp\n"
            "\n"
            "  Committed:\n"
            "  [DDR3] checkpoint\n"
            "\n"
            "› continue by docs/DDR3_PLAN.md\n"
        )

        observation = acw_loop.loop_observation_from_pane_text(
            text,
            "continue by docs/DDR3_PLAN.md",
        )

        self.assertEqual("", observation.signature)
        self.assertEqual("", observation.detail)

    def test_advance_loop_state_pauses_after_four_matching_docs_only_turns(self):
        observation = acw_loop.LoopObservation(
            signature="docs-only:docs/DDR3_PLAN.md",
            detail="docs/DDR3_PLAN.md",
        )

        state = acw_loop.advance_loop_state("docs-only:docs/DDR3_PLAN.md", 3, observation)

        self.assertEqual("docs-only:docs/DDR3_PLAN.md", state.signature)
        self.assertEqual(4, state.score)
        self.assertTrue(state.should_pause)
        self.assertIn("suspected loop: repeated docs-only checkpoint turns (4x)", state.reason)


if __name__ == "__main__":
    unittest.main()
