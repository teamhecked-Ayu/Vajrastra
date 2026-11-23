import unittest
from integer_division import ratio


class IntegerDivisionRegression(unittest.TestCase):
    def test_positive_denominator(self):
        self.assertEqual(ratio(10, 2), 5)


if __name__ == "__main__":
    unittest.main()
