import unittest

from native.system_transport import SystemTransport


class Store:
    def __init__(self):
        self.rows = {}

    def send(self, system, key, document):
        self.rows[key] = {**document, "system": system, "native_id": "1"}
        return "1"

    def find(self, system, key):
        return self.rows.get(key, {}).get("native_id")

    def read(self, system, native_id):
        return next(row for row in self.rows.values() if row["native_id"] == native_id)

    def records(self):
        return self.rows.values()


class SystemTransportTests(unittest.TestCase):
    def test_colliding_native_ids_and_retry_keys_remain_local(self):
        transport = SystemTransport()
        for name in ("plant-east", "plant-west"):
            transport.bind(name, "mes", Store())
            self.assertIsNone(transport.find(name, "retry"))
            self.assertEqual(transport.send(name, "retry", {"site": name}), "1")
        self.assertEqual(transport.read("plant-east", "1")["site"], "plant-east")
        self.assertEqual(transport.read("plant-west", "1")["site"], "plant-west")
        self.assertEqual([r["system"] for r in transport.records()], ["plant-east", "plant-west"])
        with self.assertRaises(ValueError):
            transport.bind("plant-east", "mes", Store())
        with self.assertRaises(ValueError):
            transport.find("unknown", "retry")
