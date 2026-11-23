import unittest
from swallowed_exception import parse_number


class SwallowedExceptionRegression(unittest.TestCase):
    def test_invalid_input_uses_default(self):
        self.assertEqual(parse_number("not-a-number"), 0)

    def test_valid_input_is_preserved(self):
        self.assertEqual(parse_number("42"), 42)


if __name__ == "__main__":
    unittest.main()
