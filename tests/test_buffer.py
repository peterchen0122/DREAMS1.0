import tempfile
import unittest
from pathlib import Path

from dreams_outstation.buffer import EventBufferStore
from dreams_outstation.models import BufferedEvent


class EventBufferStoreTests(unittest.TestCase):
    def test_fifo_limit_is_per_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventBufferStore(Path(tmp) / "buffer.db", per_site_limit=2)
            store.push(BufferedEvent("1", 1, "snapshot", {"seq": 1}))
            store.push(BufferedEvent("1", 1, "snapshot", {"seq": 2}))
            store.push(BufferedEvent("1", 1, "snapshot", {"seq": 3}))
            store.push(BufferedEvent("2", 1, "snapshot", {"seq": 10}))

            site1 = store.peek("1")
            self.assertEqual([row["payload"]["seq"] for row in site1], [2, 3])
            self.assertEqual(store.count("1"), 2)
            self.assertEqual(store.count("2"), 1)

    def test_ack_deletes_one_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventBufferStore(Path(tmp) / "buffer.db", per_site_limit=2)
            event_id = store.push(BufferedEvent("1", 2, "event", {"dnp_values": {"7": 100}}))
            self.assertEqual(store.count("1"), 1)
            store.ack(event_id)
            self.assertEqual(store.count("1"), 0)


if __name__ == "__main__":
    unittest.main()
