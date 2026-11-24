import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

from core.token_ledger import TokenLedger, TokenRecord, estimate_tokens


class TestTokenLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = TokenLedger(Path(self.tmp.name) / "tokens.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_measured_kept_separate_from_estimated(self):
        self.ledger.record(TokenRecord(session_id="s1", provider="gemini", model="m",
                                       input_measured=100, output_measured=50))
        self.ledger.record(TokenRecord(session_id="s1", provider="gemini", model="m",
                                       input_estimated=40, output_estimated=10))
        t = self.ledger.totals("s1")
        self.assertEqual((t["measured_input"], t["measured_output"]), (100, 50))
        self.assertEqual((t["estimated_input"], t["estimated_output"]), (40, 10))
        self.assertEqual(t["requests"], 2)

    def test_lifetime_totals_across_sessions(self):
        self.ledger.record(TokenRecord(session_id="a", provider="p", model="m",
                                       input_measured=10))
        self.ledger.record(TokenRecord(session_id="b", provider="p", model="m",
                                       input_measured=20))
        self.assertEqual(self.ledger.totals()["measured_input"], 30)
        self.assertEqual(self.ledger.totals("a")["measured_input"], 10)

    def test_budget_warning_then_hard_stop(self):
        for _ in range(5):
            self.ledger.record(TokenRecord(session_id="s2", provider="p", model="m",
                                           input_measured=100, output_measured=100))
        self.assertEqual(self.ledger.check_budget("s2", 100000), "ok")
        self.assertEqual(self.ledger.check_budget("s2", 2000, warn_fraction=0.4), "warning")
        self.assertEqual(self.ledger.check_budget("s2", 500), "exceeded")

    def test_estimate_is_only_an_estimate(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("hello world"), 0)


if __name__ == "__main__":
    unittest.main()
