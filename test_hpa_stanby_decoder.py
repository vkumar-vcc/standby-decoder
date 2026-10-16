import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("hpa_stanby_decoder.py")
BASE_LOG = """# Sending Request:
Tester -> 1D12 224721
Timestamp when shutdown was requested to ShutdownManagerHIA 1: 1791450000
Shutdown Type 1: Sleep
VCUShutdownIntent from VMM 1: StandbyWithInSystemTestRequested
ISTWakeUpTimer 1: 60
Flag indicating shutdown not successful 1: False
# Sending Request:
Tester -> 1D0D 22FE03
Time of error in Error slot 1: 2026-10-08T09:00:01
Error slot 1 - Error code: 01
"""
VEHICLE_TIME = """# Sending Request:
Tester -> 1D12 22DD0E
Complete Response: 1D12 62DD0E01D4C4BA323032362D31302D30385431303A32353A32345A
"""
RAW_HISTORY = """# Sending Request:
Tester -> 1D12 22 D9 91
Complete Response: 1D12 62D9910800000000080769EF0200000000080769FE01000000000BE1C564FF000000000BFB309D
"""


class DecoderTests(unittest.TestCase):
    def run_decoder(self, extra):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "coldboot.log"
            log.write_text(BASE_LOG + extra, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--input-file", str(log)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_existing_tables_without_history(self):
        result = self.run_decoder("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-10-08 09:00:00 UTC", result.stdout)
        self.assertIn("00:01:00", result.stdout)
        self.assertIn("FE03 error slots (non-empty)", result.stdout)
        self.assertNotIn("Platform state transition history", result.stdout)

    def test_raw_responses_and_spaced_request(self):
        result = self.run_decoder(VEHICLE_TIME + RAW_HISTORY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-10-07T16:00:16.242Z", result.stdout)
        self.assertIn("2026-10-07T16:00:16.257Z", result.stdout)
        self.assertIn("2026-10-08T09:57:38.151Z", result.stdout)
        self.assertIn("2026-10-08T10:25:24.000Z", result.stdout)
        self.assertIn("PrepareForResume", result.stdout)

    def test_decoded_fields_with_100_entries(self):
        history = "# Sending Request:\nTester -> 1D12 22D991\n"
        for index in range(1, 101):
            state = 255 if index == 100 else 1
            timestamp = 201011357 if index == 100 else 134703599 + index - 1
            history += (
                f"State ID {index}: {state} State\n"
                f"Timestamp {index}: {timestamp} ms\n"
            )
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D12 22DD0E\n"
            "Coordinated Universal Time (UTC): 2026-10-08T10:25:24Z\n"
            + history
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        table = result.stdout.split("Platform state transition history (UTC):")[1]
        self.assertEqual(sum(line.startswith("|") for line in table.splitlines()), 101)
        self.assertIn("2026-10-07T16:00:16.242Z", table)
        self.assertIn("2026-10-08T10:25:24.000Z", table)

    def test_missing_vehicle_time_fails_explicitly(self):
        result = self.run_decoder(RAW_HISTORY)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a Vehicle Time", result.stderr)

    def test_missing_or_duplicate_now_fails_explicitly(self):
        for suffix in ("", "FF000000000BFB309DFF000000000BFB309D"):
            with self.subTest(suffix=suffix):
                history = (
                    "# Sending Request:\nTester -> 1D12 22D991\n"
                    "Complete Response: 1D12 62D9910800000000080769EF"
                    + suffix + "\n"
                )
                result = self.run_decoder(VEHICLE_TIME + history)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exactly one Now", result.stderr)

    def test_truncated_history_fails_explicitly(self):
        result = self.run_decoder(
            VEHICLE_TIME + RAW_HISTORY.replace("BFB309D", "BFB30")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("9-byte records", result.stderr)

    def test_missing_decoded_timestamp_fails_explicitly(self):
        result = self.run_decoder(
            VEHICLE_TIME
            + "# Sending Request:\nTester -> 1D12 22D991\nState ID 1: 255 Now\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing timestamp", result.stderr)


if __name__ == "__main__":
    unittest.main()
