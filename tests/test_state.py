import unittest

from dreams_outstation.state import SiteState


class SiteStateTests(unittest.TestCase):
    def test_snapshot_updates_values_and_timestamp(self):
        state = SiteState("1")
        changed = state.apply_snapshot(
            {
                "ts": 1715000900,
                "reason": "periodic",
                "data": {"AI_4": 380.1, "AI_7": 52000},
            }
        )
        self.assertEqual(changed[4], 380.1)
        self.assertEqual(state.snapshot_engineering()[32], 1715000900)
        self.assertEqual(state.snapshot_dnp()[4], 38010)
        self.assertEqual(state.snapshot_dnp()[7], 52000)

    def test_control_success_bitmask(self):
        state = SiteState("1")
        state.reset_control_success()
        state.mark_control_success(1)
        state.mark_control_success(26)
        values = state.snapshot_engineering()
        self.assertEqual(values[18], 1)
        self.assertEqual(values[19], 1)

    def test_class2_skips_non_deadband_points(self):
        state = SiteState("1")
        changed = state.apply_event({"ts": 1, "reason": "deadband", "data": {"AI_11": 10, "AI_7": 20}})
        dnp_values = state.dnp_values_for_changed(changed)
        self.assertNotIn(11, dnp_values)
        self.assertEqual(dnp_values[7], 20)

    def test_status_updates_online_state_without_ai_mapping(self):
        state = SiteState("1", include_spare_point_31=True)

        offline = state.apply_status({"ts": 1715000900, "status": "offline"})
        online = state.apply_status({"ts": 1715000910, "online": True})

        self.assertFalse(offline)
        self.assertTrue(online)
        self.assertEqual(state.snapshot_engineering()[31], 0.0)
        self.assertEqual(state.snapshot_engineering()[32], 1715000910)

    def test_unknown_status_payload_does_not_change_online_state(self):
        state = SiteState("1", include_spare_point_31=True)
        state.apply_status({"status": "online"})

        changed = state.apply_status({"message": "heartbeat"})

        self.assertIsNone(changed)
        self.assertTrue(state.online)


if __name__ == "__main__":
    unittest.main()
