import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from native.software_connections import connection


class Connections(unittest.TestCase):
    def test_pinning_and_instance_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "connections.json"
            target = dict(url="http://localhost:8123", site="test", company="C",
                          customer="A", item="I", warehouse="W")
            profile = dict(adapter="erpnext-sales", token_env="TEST_ONLY_SALES", erp=target)
            path.write_text(json.dumps({"sales": profile}))
            with patch.dict(os.environ, NORVALE_CONNECTIONS_FILE=str(path), TEST_ONLY_SALES="offline"):
                spec = {"connection": "sales"}
                fingerprint, east = connection("run", "east", spec)
                _, west = connection("run", "west", spec, expected=fingerprint)
                self.assertNotEqual(east.prefix, west.prefix)
                with self.assertRaises(ValueError):
                    connection("run", "east", {**spec, "url": "https://invalid.test"})
                target["site"] = "changed"
                path.write_text(json.dumps({"sales": profile}))
                with self.assertRaisesRegex(ValueError, "target changed"):
                    connection("run", "east", spec, expected=fingerprint)
