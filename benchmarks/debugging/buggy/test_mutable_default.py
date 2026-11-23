import unittest
from mutable_default import collect


class MutableDefaultRegression(unittest.TestCase):
    def test_calls_do_not_share_state(self):
        self.assertEqual(collect(1), [1])
        self.assertEqual(collect(2), [2])


if __name__ == "__main__":
    unittest.main()
