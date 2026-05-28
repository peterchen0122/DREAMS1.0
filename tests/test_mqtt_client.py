import unittest

from dreams_outstation.mqtt_client import _reason_code_value


class ReasonCodeValueTests(unittest.TestCase):
    def test_accepts_int_reason_code(self):
        self.assertEqual(_reason_code_value(0), 0)

    def test_accepts_paho_v2_reason_code_shape(self):
        class ReasonCode:
            value = 0

        self.assertEqual(_reason_code_value(ReasonCode()), 0)


if __name__ == "__main__":
    unittest.main()
