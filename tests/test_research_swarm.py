import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from engines.research_swarm import (Source, add_claim, comparison_table,
                                    synthesize)


class TestResearchSwarm(unittest.TestCase):
    def test_supported_and_unsupported_claims_distinct(self):
        claims = []
        add_claim(claims, "method A beats B on the benchmark",
                  [Source("https://example.com/report", "Benchmark Report")])
        add_claim(claims, "unverifiable rumor")
        syn = synthesize("Findings", claims)
        self.assertIn("https://example.com/report", syn.markdown)
        self.assertIn("UNVERIFIED", syn.markdown)
        self.assertEqual(syn.supported, 1)
        self.assertEqual(syn.unsupported, 1)

    def test_partial_when_source_url_missing(self):
        claims = []
        add_claim(claims, "vague", [Source("")])
        self.assertEqual(claims[0].status, "unsupported")

    def test_measured_table_keeps_commentary_out(self):
        rows = [{"run": 1, "seed": 42, "measured": 0.91,
                 "notes": "model commentary stays out of the table"}]
        table = comparison_table(rows)
        self.assertIn("0.91", table)
        self.assertNotIn("commentary", table)

    def test_empty_table_is_honest(self):
        self.assertIn("no measured results", comparison_table([]))


if __name__ == "__main__":
    unittest.main()
