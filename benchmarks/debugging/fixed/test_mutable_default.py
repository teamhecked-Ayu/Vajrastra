import unittest
from mutable_default import collect


class MutableDefaultRegression(unittest.TestCase):
    def test_calls_do_not_share_state(self):
        self.assertEqual(collect(1), [1])
        self.assertEqual(collect(2), [2])

    def test_explicit_list_is_reused(self):
        items = [0]
        self.assertIs(collect(1, items), items)
        self.assertEqual(items, [0, 1])


if __name__ == "__main__":
    unittest.main()
