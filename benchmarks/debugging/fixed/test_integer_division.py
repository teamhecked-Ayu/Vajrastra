import unittest
from integer_division import ratio


class IntegerDivisionRegression(unittest.TestCase):
    def test_positive_denominator(self):
        self.assertEqual(ratio(10, 2), 5)

    def test_zero_denominator_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "count must not be zero"):
            ratio(10, 0)


if __name__ == "__main__":
    unittest.main()
