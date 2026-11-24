import unittest
from swallowed_exception import parse_number


class SwallowedExceptionRegression(unittest.TestCase):
    def test_invalid_input_must_not_return_none(self):
        self.assertEqual(parse_number("not-a-number"), 0)


if __name__ == "__main__":
    unittest.main()
