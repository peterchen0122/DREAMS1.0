import unittest

from dreams_outstation.points import AI_POINTS, AO_POINTS, build_mqtt_command, enabled_ai_points


class PointTableTests(unittest.TestCase):
    def test_ai_scaling_uses_taipower_appendix_units(self):
        self.assertEqual(AI_POINTS[4].to_dnp_value(380.1), 38010)
        self.assertEqual(AI_POINTS[7].to_dnp_value(52000), 52000)
        self.assertEqual(AI_POINTS[10].to_dnp_value(60.01), 600)
        self.assertEqual(AI_POINTS[20].to_dnp_value(0.5), 50)

    def test_ai_11_uses_appendix_var6(self):
        self.assertEqual(AI_POINTS[11].static_variation, 6)
        self.assertEqual(AI_POINTS[11].event_variation, 6)

    def test_spare_31_is_disabled_by_default(self):
        self.assertNotIn(31, enabled_ai_points())
        self.assertIn(31, enabled_ai_points(include_spare_point_31=True))
        self.assertEqual(AI_POINTS[31].name, "Spare")

    def test_deadband_ao_raw_value_is_001_percent(self):
        payload = build_mqtt_command(12, 250, "cmd-1")
        self.assertEqual(payload["type"], "config_deadband")
        self.assertEqual(payload["target"], "Deadband_AI_7")
        self.assertEqual(payload["value"], 2.5)
        self.assertEqual(payload["unit"], "%")

    def test_ao_feedback_mapping(self):
        self.assertEqual(AO_POINTS[1].feedback_ai, 15)
        self.assertEqual(AO_POINTS[3].feedback_ai, 17)
        self.assertEqual(AO_POINTS[12].feedback_ai, 27)


if __name__ == "__main__":
    unittest.main()
