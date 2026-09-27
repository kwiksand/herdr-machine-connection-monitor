import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapter import HerdrCliAdapter  # noqa: E402


class TestHerdrCliAdapter(unittest.TestCase):
    @patch.object(HerdrCliAdapter, "_run_cli")
    def test_list_machines_parsing(self, mock_run_cli):
        json_output = """
[
  {
    "id": "8724cfc1531e3d6542673387623b8a52",
    "label": "server2",
    "target": "server2",
    "session": "default",
    "enabled": true,
    "selected": false
  },
  {
    "id": "981b368cf2547b1c1e1141e2d039dc94",
    "label": "server3",
    "target": "server3",
    "session": "default",
    "enabled": false,
    "selected": false
  }
]
"""
        mock_run_cli.return_value = subprocess.CompletedProcess(
            args=["herdr", "machine", "list", "--json"],
            returncode=0,
            stdout=json_output,
            stderr="",
        )

        adapter = HerdrCliAdapter(herdr_bin="herdr")
        machines = adapter.list_machines()

        self.assertEqual(len(machines), 2)
        self.assertEqual(machines[0].id, "8724cfc1531e3d6542673387623b8a52")
        self.assertEqual(machines[0].label, "server2")
        self.assertTrue(machines[0].enabled)
        self.assertEqual(machines[1].id, "981b368cf2547b1c1e1141e2d039dc94")
        self.assertFalse(machines[1].enabled)

    @patch.object(HerdrCliAdapter, "_run_cli")
    def test_disable_machine_call(self, mock_run_cli):
        mock_run_cli.return_value = subprocess.CompletedProcess(
            args=["herdr", "machine", "disable", "8724cfc1531e3d6542673387623b8a52"],
            returncode=0,
            stdout="Disabled SSH machine server2",
            stderr="",
        )

        adapter = HerdrCliAdapter()
        ok = adapter.disable_machine("8724cfc1531e3d6542673387623b8a52")
        self.assertTrue(ok)
        mock_run_cli.assert_called_once_with(
            ["machine", "disable", "8724cfc1531e3d6542673387623b8a52"],
            timeout=10.0,
        )


if __name__ == "__main__":
    unittest.main()
