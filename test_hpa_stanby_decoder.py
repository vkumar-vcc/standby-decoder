import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from error_reference import (
    DEFAULT_REFERENCE,
    build_error_reference,
    fe_03_reference_values,
    load_error_reference,
)


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
    def run_decoder(self, extra, *options, columns=161):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "coldboot.log"
            log.write_text(BASE_LOG + extra, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--input-file", str(log), *options],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "COLUMNS": str(columns)},
            )

    def run_with_reference(
        self, sheets, reporter="E0 01", code="00 00 2D E2", columns=161,
    ):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.json"
            reference.write_text(json.dumps(sheets), encoding="utf-8")
            return self.run_decoder(
                f"Error slot 1 - Reporter ID: {reporter}\n"
                f"Error slot 1 - Error code: {code}\n",
                "--error-reference", str(reference),
                columns=columns,
            )

    def fe03_lines(self, result, marker="FE03 error slots (non-empty):"):
        if marker not in result.stdout:
            return []
        table = result.stdout.split(marker)[1].splitlines()
        start = next(i for i, line in enumerate(table) if line.startswith("+-"))
        end = next(
            (i for i in range(start, len(table)) if not table[i].startswith(("+-", "| "))),
            len(table),
        )
        return table[start:end]

    def parse_table(self, lines):
        bounds = [index for index, character in enumerate(lines[0]) if character == "+"]
        header_end = next(
            index for index in range(1, len(lines)) if lines[index].startswith("+")
        )
        blocks = [[]]
        for position, line in enumerate(lines[1:], 1):
            if line.startswith("+"):
                continue
            cells = [
                line[start + 2:end - 1].strip()
                for start, end in zip(bounds, bounds[1:])
            ]
            if position > header_end and cells[0]:
                blocks.append([])
            blocks[-1].append(cells)
        return [
            [" ".join(part for part in column if part) for column in zip(*block)]
            for block in blocks
        ]

    def reference_rows(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        slots = self.parse_table(self.fe03_lines(result))
        details = self.fe03_lines(result, "FE03 error reference details:")
        if not details:
            return slots
        details = self.parse_table(details)
        order = [
            "Slot", "Time", "Standby #", "Reporter ID", "Error Code",
            "Error Attribute", "SOC_ERROR", "HP Operation State", "Occurrence",
            "Error Name", "Description", "Error Level", "Expected SOC_ERROR",
            "HI Action", "HP Action", "Reference",
        ]
        self.assertEqual(
            [row[0] for row in slots[1:]], [row[0] for row in details[1:]]
        )
        headers = slots[0] + details[0][1:]
        merged = [
            dict(zip(headers, slot + detail[1:], strict=True))
            for slot, detail in zip(slots, details, strict=True)
        ]
        columns = [header for header in order if header in headers]
        return [[row[header] for header in columns] for row in merged]

    def assert_wrapped_table(self, result, columns=160):
        lines = self.fe03_lines(result) + self.fe03_lines(
            result, "FE03 error reference details:"
        )
        self.assertLessEqual(max(map(len, lines)), columns)
        return lines

    def test_fe03_reference_exact_match_preserves_original_columns(self):
        result = self.run_with_reference({"HW_BPMP": {
            "row_numbers": [5],
            "rows": [{
                "Reporter ID": "0xE001", "Error Code": "0x2de2",
                "Error Name": "BPMP_CLUSTER_CE", "Error Level": 0,
                "Description": "Corrected BPMP error",
                "Asserts SOC_ERROR": "No", "HI": "Log\nerror", "HP": "No action",
            }],
        }})
        rows = self.reference_rows(result)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][-7:], [
            "Error Name", "Description", "Error Level", "Expected SOC_ERROR",
            "HI Action", "HP Action", "Reference",
        ])
        self.assertEqual(rows[1][:9], [
            "1", "2026-10-08T09:00:01", "1", "0xE001", "0x2DE2",
            "", "", "", "",
        ])
        self.assertEqual(rows[1][-7:], [
            "BPMP_CLUSTER_CE", "Corrected BPMP error", "0", "No",
            "Log error", "No action", "HW_BPMP:5",
        ])

    def test_fe03_reference_normalizes_numeric_and_hex_identifiers(self):
        for reporter, code in [
            ("0xe001", "0x00002DE2"), ("E001", "2de2"),
            ("E0 01", "00 00 2d e2"),
        ]:
            with self.subTest(reporter=reporter, code=code):
                result = self.run_with_reference({"HW": {"rows": [{
                    "Reporter ID": 0xE001, "Error code": 0x2DE2,
                    "Error name": "MATCHED",
                }]}}, reporter, code)
                self.assertEqual(self.reference_rows(result)[1][-7], "MATCHED")

    def test_fe03_reference_requires_both_identifiers(self):
        reference = {"HW": {"rows": [{
            "Reporter ID": "0xE001", "Error Code": "0x2de2", "Error Name": "MATCHED",
        }]}}
        for reporter, code in [
            ("E002", "2de2"), ("E001", "2de3"), ("", "2de2"),
            ("invalid", "2de2"),
        ]:
            with self.subTest(reporter=reporter, code=code):
                rows = self.reference_rows(self.run_with_reference(reference, reporter, code))
                self.assertEqual(rows[1][-7:], [
                    "Not found", "N/A", "N/A", "N/A", "N/A", "N/A", "No exact match",
                ])

    def test_fe03_reference_multiple_matches_remain_one_slot(self):
        result = self.run_with_reference({
            name: {"rows": [{
                "Reporter ID": "E001", "Error Code": "2de2", "Error Name": name,
                "Error Level": level,
            }]}
            for name, level in [("HW_A", 0), ("HW_B", 2)]
        })
        rows = self.reference_rows(result)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][-7], "[1] HW_A; [2] HW_B")
        self.assertEqual(rows[1][-5], "[1] 0; [2] 2")
        self.assertEqual(rows[1][-1], "[1] HW_A; [2] HW_B")

    def test_fe03_reference_column_letter_headers_and_placeholders(self):
        result = self.run_with_reference({"HW_MGBE": {
            "row_numbers": [2, 4, 5, 8, 9, 10],
            "rows": [
                {"B": "Title"},
                {"B": "Error Name", "C": "Reporter ID", "D": "ErrorCode", "I": "Error Level"},
                {"B": "MATCHED", "C": "0xE001", "D": "0x2de2", "I": 1},
                {"B": "Error Name", "C": "Reporter ID", "D": "ErrorCode", "I": "Error Level"},
                {"B": "Placeholder", "C": ".", "D": "."},
                {"B": "Template", "C": "0x811Z", "D": "0xC2XXXXXX"},
            ],
        }})
        row = self.reference_rows(result)[1]
        self.assertEqual(row[-7:], [
            "MATCHED", "N/A", "1", "N/A", "N/A", "N/A", "HW_MGBE:5",
        ])

    def test_fe03_long_values_wrap_without_losing_content(self):
        description = " ".join(["A detailed error description."] * 30)
        action = " ".join(["Reset HP only according to the defined policy."] * 10)
        long_name = "LONG_ERROR_" + "X" * 180
        for columns in [161, 101]:
            with self.subTest(columns=columns):
                result = self.run_with_reference({"HW": {"rows": [{
                    "Reporter ID": "E001", "Error Code": "2de2",
                    "Error Name": long_name, "Description": description, "HP": action,
                }]}}, columns=columns)
                lines = self.assert_wrapped_table(result, columns - 1)
                self.assertGreater(len(lines), 10)
                table = "\n".join(lines)
                row = self.reference_rows(result)[1]
                self.assertEqual(row[-6], description)
                self.assertEqual(row[-2], action)
                self.assertEqual(row[-7].replace(" ", ""), long_name)
                self.assertNotIn("...", table)

    def test_fe03_many_alternatives_wrap_within_columns(self):
        result = self.run_with_reference({
            f"HW_{number}": {"rows": [{
                "Reporter ID": "E001", "Error Code": "2de2",
                "Error Name": f"ERROR_{number}", "HP": f"Action {number}",
            }]} for number in range(1, 11)
        })
        self.assert_wrapped_table(result)
        rows = self.reference_rows(result)
        self.assertEqual(len(rows), 2)
        for number in range(1, 11):
            self.assertIn(f"[{number}] ERROR_{number}", rows[1][-7])
            self.assertIn(f"[{number}] Action {number}", rows[1][-2])
            self.assertIn(f"[{number}] HW_{number}", rows[1][-1])

    def test_fe03_multiple_slots_are_separate_table_rows(self):
        result = self.run_decoder(
            "Error slot 1 - Reporter ID: E0 01\n"
            "Error slot 1 - Error code: 2DE2\n"
            "Time of error in Error slot 2: 2026-10-08T09:00:02\n"
            "Error slot 2 - Reporter ID: 9F 0D\n"
            "Error slot 2 - Error code: 0001\n"
        )
        rows = self.reference_rows(result)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row[0] for row in rows[1:]], ["1", "2"])
        self.assertEqual(rows[1][-7], "BPMP_CLUSTER_CE")
        self.assertEqual(rows[2][-7], "MCUFOEM_SOC_ASSERTION_MISMATCH")
        self.assertIn("SOC pin", rows[2][-6])

    def test_fe03_identifiers_show_compact_hex(self):
        result = self.run_decoder(
            "Error slot 1 - Reporter ID: 81 34\n"
            "Error slot 1 - Error code: 00 00 02 00\n"
            "Error slot 1 - Error attribute: 00 00 02 2A\n"
            "Time of error in Error slot 2: 2026-10-08T09:00:02\n"
            "Error slot 2 - Reporter ID: invalid\n"
            "Error slot 2 - Error code: C0 83 A8 3C\n"
            "Error slot 2 - Error attribute: 00 00 00 00\n",
            "--no-error-reference",
        )
        rows = self.reference_rows(result)
        self.assertEqual(rows[1][3:6], ["0x8134", "0x200", "0x22A"])
        self.assertEqual(rows[2][3:6], ["invalid", "0xC083A83C", "0x0"])

    def test_fe03_reference_does_not_guess_bit_fields_or_templates(self):
        result = self.run_with_reference({"SW": {"rows": [
            {"Reporter ID": "E001", "Error Code (bit 31-26)": 1, "Error Name": "BITS"},
            {"Reporter ID": "E001", "Error Code": "0x04XXXXXX", "Error Name": "TEMPLATE"},
            {"Reporter ID": "E002", "Error Code": "0x2de2", "Error Name": "OTHER"},
        ]}}, code="04000000")
        self.assertEqual(self.reference_rows(result)[1][-7], "Not found")

    def test_fe03_reference_invalid_data_fails_explicitly(self):
        for data in [
            [], {"HW": {}}, {"HW": {"rows": ["invalid"]}},
            {"HW": {"rows": [], "row_numbers": [1]}},
            {"HW": {"rows": []}},
        ]:
            with self.subTest(data=data):
                result = self.run_with_reference(data)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot load error reference", result.stderr)

    def test_fe03_reference_missing_or_malformed_file_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference.json"
            for content in [None, "{invalid json"]:
                with self.subTest(content=content):
                    if content is not None:
                        reference.write_text(content, encoding="utf-8")
                    result = self.run_decoder("", "--error-reference", str(reference))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Cannot load error reference", result.stderr)

    def test_existing_tables_without_history(self):
        result = self.run_decoder("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-10-08 09:00:00 UTC", result.stdout)
        self.assertIn("00:01:00", result.stdout)
        self.assertIn("FE03 error slots (non-empty)", result.stdout)
        self.assertIn("Expected SOC_ERROR", result.stdout)
        self.assertIn("Description", result.stdout)
        self.assertNotIn("Platform state transition history", result.stdout)
        self.assertNotIn("HIA DTC snapshot summary", result.stdout)

    def test_reference_enrichment_can_be_disabled(self):
        result = self.run_decoder("", "--no-error-reference")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Expected SOC_ERROR", result.stdout)
        self.assertIn("FE03 error slots (non-empty)", result.stdout)

    def test_bundled_reference_description_and_hp_action(self):
        result = self.run_decoder(
            "Error slot 1 - Reporter ID: E0 01\n"
            "Error slot 1 - Error code: 00 00 2D E2\n"
        )
        row = self.reference_rows(result)[1]
        self.assertEqual(row[-7], "BPMP_CLUSTER_CE")
        self.assertTrue(row[-6].startswith("Safety mechanism:"))
        self.assertEqual(row[-2], "No action")

    def test_raw_responses_and_spaced_request(self):
        result = self.run_decoder(VEHICLE_TIME + RAW_HISTORY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-10-07T16:00:16.242Z", result.stdout)
        self.assertIn("2026-10-07T16:00:16.257Z", result.stdout)
        self.assertIn("2026-10-08T09:57:38.151Z", result.stdout)
        self.assertIn("2026-10-08T10:25:24.000Z", result.stdout)
        self.assertIn("PrepareForResume", result.stdout)
        table = result.stdout.split("Platform state transition history (UTC):")[1]
        rows = [
            [cell.strip() for cell in line.split("|")[1:-1]]
            for line in table.splitlines() if line.startswith("|")
        ]
        self.assertEqual(rows[0][-1], "Time taken (HH:MM:SS.mmm)")
        self.assertEqual(
            [row[-1] for row in rows[1:]],
            ["N/A", "00:00:00.015", "17:57:21.894", "00:27:45.849"],
        )

    def test_hia_dtc_summary_deduplicates_dtcs_and_converts_latest_global_time(self):
        hia_log = (
            "# Sending Request:\nTester -> 1D0D 1903\n"
            "Complete Response: 1D0D 5903F0006820F0006821F04A6821\n"
            "F00068\tControl Module- Algorithm Based Failures- Event information"
            "\t20\tSnapshot Datarecord\n"
            "F00068\tControl Module- Algorithm Based Failures- Event information"
            "\t21\tSnapshot Datarecord\n"
            "F04A68\tHigh Computation Control Module A- Algorithm Based Failures"
            "\t21\tSnapshot Datarecord\n"
            "# Sending Request:\nTester -> 1D0D 1904F00068FF\n"
            "Complete Response: 1D0D 5904F00068\n"
            "Global Real Time: 435500.000000 min\n"
            "Global Real Time: 435697.533333 min\n"
            "Latest event which caused F00068: 1 DEM_EV_MCU_RESET_LAST_CYC\n"
            "# Sending Request:\nTester -> 1D0D 1904F04A68FF\n"
            "Complete Response: 1D0D 5904F04A68\n"
            "Global Real Time: 435600.000000 min\n"
            "Latest event which caused F04A68: 2 DEM_EV_HPA_FAILURE\n"
        )
        vehicle_time = (
            "# Sending Request:\nTester -> 1D12 22DD0E\n"
            "Global Real Time: 435884.766667 min\n"
            "Coordinated Universal Time (UTC): 2026-10-09T14:39:24Z\n"
        )
        result = self.run_decoder(vehicle_time + hia_log)
        self.assertEqual(result.returncode, 0, result.stderr)
        table = result.stdout.split("HIA DTC snapshot summary:")[1]
        rows = [
            [cell.strip() for cell in line.split("|")[1:-1]]
            for line in table.splitlines() if line.startswith("|")
        ]
        self.assertEqual(rows, [
            ["DTC", "Description", "Latest Global Real Time", "UTC Time", "Latest Event"],
            [
                "F00068",
                "Control Module- Algorithm Based Failures- Event information",
                "435697.533333 min",
                "2026-10-09T11:32:10.000Z",
                "1 DEM_EV_MCU_RESET_LAST_CYC",
            ],
            [
                "F04A68",
                "High Computation Control Module A- Algorithm Based Failures",
                "435600.000000 min",
                "2026-10-09T09:54:38.000Z",
                "2 DEM_EV_HPA_FAILURE",
            ],
        ])

    def test_hia_dtc_timestamps_require_vehicle_time_anchor(self):
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D0D 1903\n"
            "Complete Response: 1D0D 5903F0006820\n"
            "# Sending Request:\nTester -> 1D0D 1904F00068FF\n"
            "Global Real Time: 435697.533333 min\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("require Global Real Time and UTC", result.stderr)

    def test_hib_and_hpa_dtc_summaries_use_the_vehicle_time_anchor(self):
        ecu_log = (
            "# Sending Request:\nTester -> 1D0E 1903\n"
            "Complete Response: 1D0E "
            "5903E10768211A0B682195E3682127936821459768210A906821"
            "0A9168210A7868210A796821D4E56821D4E6682110326821\n"
            "E10768\tCollision Mitigation By Braking- Algorithm Based Failures"
            "- Event information\t21\tSnapshot Datarecord\n"
            "1A0B68\tHybrid Battery Power Limit Exceeded- Algorithm Based Failures"
            "- Event information\t21\tSnapshot Datarecord\n"
            "# Sending Request:\nTester -> 1D0E 1904E10768FF\n"
            "Complete Response: 1D0E 5904E10768\n"
            "Global Real Time: 435697.533333 min\n"
            "Latest event which caused E10768: 1 DEM_EV_BRAKE_FAILURE\n"
            "# Sending Request:\nTester -> 1D12 1903\n"
            "Complete Response: 1D12 "
            "5903D51B8721D00B6821C82A8C21C7988C2199B86821343E1D21"
            "E1015620E1015621\n"
            "D51B87\tWireless Phone Charger (WPC)- Bus Signal / Message Failures"
            "- Missing message\t21\tSnapshot Datarecord\n"
            "E10156\tControl Module Configuration Incompatible- System Programming"
            " Failures- Invalid / incompatible configuration\t20\tSnapshot Datarecord\n"
            "E10156\tControl Module Configuration Incompatible- System Programming"
            " Failures- Invalid / incompatible configuration\t21\tSnapshot Datarecord\n"
            "# Sending Request:\nTester -> 1D12 1904D51B87FF\n"
            "Complete Response: 1D12 5904D51B87\n"
            "Global Real Time: 435600.000000 min\n"
            "Latest event which caused D51B87: 2 DEM_EV_WPC_FAILURE\n"
        )
        vehicle_time = (
            "# Sending Request:\nTester -> 1D12 22DD0E\n"
            "Global Real Time: 435884.766667 min\n"
            "Coordinated Universal Time (UTC): 2026-10-09T14:39:24Z\n"
        )
        result = self.run_decoder(vehicle_time + ecu_log)
        self.assertEqual(result.returncode, 0, result.stderr)
        for ecu, dtc, description, utc, event in [
            (
                "HIB", "E10768",
                "Collision Mitigation By Braking- Algorithm Based Failures- Event information",
                "2026-10-09T11:32:10.000Z", "1 DEM_EV_BRAKE_FAILURE",
            ),
            (
                "HPA", "D51B87",
                "Wireless Phone Charger (WPC)- Bus Signal / Message Failures- Missing message",
                "2026-10-09T09:54:38.000Z", "2 DEM_EV_WPC_FAILURE",
            ),
        ]:
            with self.subTest(ecu=ecu):
                table = result.stdout.split(f"{ecu} DTC snapshot summary:")[1]
                rows = [
                    [cell.strip() for cell in line.split("|")[1:-1]]
                    for line in table.splitlines() if line.startswith("|")
                ]
                self.assertEqual(rows[1][0], dtc)
                self.assertEqual(rows[1][1], description)
                self.assertEqual(rows[1][3:], [utc, event])
        hpa_table = result.stdout.split("HPA DTC snapshot summary:")[1]
        hpa_rows = [
            [cell.strip() for cell in line.split("|")[1:-1]]
            for line in hpa_table.splitlines() if line.startswith("|")
        ]
        self.assertEqual([row[0] for row in hpa_rows[1:]], [
            "D51B87", "D00B68", "C82A8C", "C7988C",
            "99B868", "343E1D", "E10156",
        ])

    def test_watchdog_raw_and_decoded_timelines(self):
        timeline = (
            "zen_qm_vision,1970-01-01T00:01:21\n"
            "usage-modes-manager,2026-07-28T00:09:39\n"
            "usage-modes-manager,2026-07-28T00:09:39\n"
        )
        for body in [
            "Complete Response: 1D12 62D99D0100"
            + (timeline.encode("ascii") + b"\x00" * 32).hex() + "\n",
            "Data structure version: 1\nData Health: 0 No error\n"
            "Application name and timeline: " + timeline,
        ]:
            with self.subTest(body=body):
                result = self.run_decoder(
                    "# Sending Request:\nTester -> 1D12 22 D9 9D\n" + body,
                    "--watchdog-details",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                table = result.stdout.split(
                    "HPA application watchdog failure timeline:"
                )[1]
                rows = [
                    [cell.strip() for cell in line.split("|")[1:-1]]
                    for line in table.splitlines() if line.startswith("|")
                ]
                self.assertEqual(rows[1:], [
                    ["1", "zen_qm_vision", "1970-01-01T00:01:21"],
                    ["2", "usage-modes-manager", "2026-07-28T00:09:39"],
                    ["3", "usage-modes-manager", "2026-07-28T00:09:39"],
                ])
                self.assertIn("unset clock", result.stdout)
                self.assertIn("timezone is not specified", result.stdout)

    def test_watchdog_summary_counts_and_dates(self):
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D12 22D99D\n"
            "Application name and timeline: app_b,1970-01-01T00:01:21\n"
            "app_a,2026-07-28T00:09:39\n"
            "app_a,2026-07-27T00:09:39\n"
            "app_a,2026-07-28T00:09:39\n"
            "app_a,1970-01-01T00:01:21\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        table = result.stdout.split("HPA application watchdog failure summary:")[1]
        rows = [
            [cell.strip() for cell in line.split("|")[1:-1]]
            for line in table.splitlines() if line.startswith("|")
        ][1:]
        self.assertEqual(rows, [
            ["app_a", "4", "2026-07-27T00:09:39", "2026-07-28T00:09:39", "1"],
            ["app_b", "1", "N/A", "N/A", "1"],
        ])
        self.assertIn("5 failure entries across 2 applications", table)
        self.assertNotIn("HPA application watchdog failure timeline:", result.stdout)

    def test_watchdog_empty_timeline(self):
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D12 22D99D\n"
            "Complete Response: 1D12 62D99D0100\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No watchdog failure entries recorded.", result.stdout)

    def test_watchdog_invalid_entry_fails_explicitly(self):
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D12 22D99D\n"
            "Application name and timeline: invalid-entry\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid watchdog timeline entry", result.stderr)

    def test_watchdog_no_data_preserves_other_tables(self):
        for body in [
            "Complete Response: 1D12 62D99D010300000000\n",
            "Data structure version: 1\nData Health: 3 No data avaliable\n"
            "Application name and timeline:\n",
        ]:
            with self.subTest(body=body):
                result = self.run_decoder(
                    VEHICLE_TIME + RAW_HISTORY
                    + "# Sending Request:\nTester -> 1D12 22D99D\n" + body,
                    "--watchdog-details",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Data Health 3 (No data available)", result.stdout)
                self.assertIn("Platform state transition history (UTC):", result.stdout)
                self.assertIn("FE03 error slots (non-empty)", result.stdout)
                self.assertNotIn("Total: 0 failure", result.stdout)
                self.assertNotIn("No watchdog failure entries recorded.", result.stdout)

    def test_watchdog_overloaded_decodes_with_warning(self):
        timeline = "app_a,2026-07-28T00:09:39\n"
        for body in [
            "Complete Response: 1D12 62D99D0102" + timeline.encode("ascii").hex() + "\n",
            "Data structure version: 1\nData Health: 2 Overloaded\n"
            "Application name and timeline: " + timeline,
        ]:
            with self.subTest(body=body):
                result = self.run_decoder(
                    "# Sending Request:\nTester -> 1D12 22D99D\n" + body
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Data Health 2 (Overloaded)", result.stdout)
                self.assertIn("Total: 1 failure entries", result.stdout)

    def test_watchdog_corrupt_data_still_fails(self):
        result = self.run_decoder(
            "# Sending Request:\nTester -> 1D12 22D99D\n"
            "Complete Response: 1D12 62D99D0101\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data health error: 1", result.stderr)

    def test_rows_after_now_are_hidden(self):
        histories = [
            RAW_HISTORY.rstrip() + "01000000000BFB309E\n",
            "# Sending Request:\nTester -> 1D12 22D991\n"
            "State ID 1: 1 Run\nTimestamp 1: 199345508 ms\n"
            "State ID 2: 255 Now\nTimestamp 2: 201011357 ms\n"
            "State ID 3: 2 Standby\nTimestamp 3: 201011358 ms\n",
        ]
        for history, expected_count in zip(histories, [4, 2]):
            with self.subTest(history=history):
                result = self.run_decoder(VEHICLE_TIME + history)
                self.assertEqual(result.returncode, 0, result.stderr)
                table = result.stdout.split(
                    "Platform state transition history (UTC):"
                )[1]
                rows = [
                    [cell.strip() for cell in line.split("|")[1:-1]]
                    for line in table.splitlines() if line.startswith("|")
                ][1:]
                self.assertEqual(len(rows), expected_count)
                self.assertEqual(rows[-1][1:], [
                    "Now", "2026-10-08T10:25:24.000Z", "00:27:45.849"
                ])

    def test_decoded_elapsed_zero_and_over_24_hours(self):
        history = "# Sending Request:\nTester -> 1D12 22D991\n"
        for index, state, timestamp in [
            (1, 0, 0), (2, 3, 0), (3, 255, 90061001)
        ]:
            history += (
                f"State ID {index}: {state} State\n"
                f"Timestamp {index}: {timestamp} ms\n"
            )
        result = self.run_decoder(VEHICLE_TIME + history)
        self.assertEqual(result.returncode, 0, result.stderr)
        table = result.stdout.split("Platform state transition history (UTC):")[1]
        durations = [
            line.split("|")[-2].strip()
            for line in table.splitlines() if line.startswith("|")
        ][1:]
        self.assertEqual(durations, ["N/A", "00:00:00.000", "25:01:01.001"])

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


class ErrorReferenceTests(unittest.TestCase):
    def test_sqlite_matches_json_and_retains_all_alternatives(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            destination = Path(directory) / "reference.sqlite3"
            source.write_text(json.dumps({
                "HW": {
                    "row_numbers": [5, 6, 7],
                    "rows": [
                        {
                            "Reporter ID": "0xE001", "Error Code": "0x2de2",
                            "Error Name": "FIRST", "Description": "Error description",
                            "HP": "No action", "Reviewer": "Not stored",
                        },
                        {
                            "Reporter ID": "0xE001", "Error Code": "0x2de2",
                            "Error Name": "SECOND", "Error Level": 0,
                            "safety Mechanism name": "ECC\nmonitor",
                        },
                        {"Reporter ID": "0x811Z", "Error Code": "0xC2XXXXXX"},
                    ],
                },
            }), encoding="utf-8")
            self.assertEqual(build_error_reference(source, destination), (2, 1))
            json_reference = load_error_reference(source)
            database = load_error_reference(destination)
            try:
                for record in [
                    {"reporter_id": "E0 01", "error_code": "00 00 2D E2"},
                    {"reporter_id": "E0 02", "error_code": "2de2"},
                    {},
                ]:
                    self.assertEqual(
                        fe_03_reference_values(record, database),
                        fe_03_reference_values(record, json_reference),
                    )
                self.assertEqual(
                    database.execute("PRAGMA integrity_check").fetchone(), ("ok",)
                )
                plan = database.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM errors "
                    "WHERE reporter_id = ? AND error_code = ?", (0xE001, 0x2DE2)
                ).fetchall()
                self.assertIn("error_pair", str(plan))
                columns = [
                    row[1] for row in database.execute("PRAGMA table_info(errors)")
                ]
                self.assertNotIn("Reviewer", columns)
                with self.assertRaises(sqlite3.OperationalError):
                    database.execute("DELETE FROM errors")
            finally:
                database.close()

    def test_invalid_source_does_not_replace_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            destination = Path(directory) / "reference.sqlite3"
            destination.write_bytes(b"existing database")
            source.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_error_reference(source, destination)
            self.assertEqual(destination.read_bytes(), b"existing database")
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), [
                "reference.sqlite3", "source.json",
            ])

    def test_database_version_and_schema_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.sqlite3"
            connection = sqlite3.connect(path)
            connection.close()
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                load_error_reference(path)
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 2")
            connection.close()
            with self.assertRaises(sqlite3.OperationalError):
                load_error_reference(path)

    def test_bundled_database_narrows_by_error_attribute(self):
        database = load_error_reference(DEFAULT_REFERENCE)
        try:
            record = {"reporter_id": "81 34", "error_code": "00 00 02 00"}
            self.assertEqual(
                fe_03_reference_values(
                    {**record, "error_attribute": "00 00 02 2A"}, database
                )[0],
                "NVTIME_GTS_EPL_DISCON_PULSE",
            )
            self.assertIn(
                "[23]", fe_03_reference_values(record, database)[0]
            )
        finally:
            database.close()

    def test_error_attribute_narrows_matches_and_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            destination = Path(directory) / "reference.sqlite3"
            source.write_text(json.dumps({"SW": {"rows": [
                {"Reporter ID": "0x8134", "Error Code": "0x200",
                 "Meta Data": "0x220", "Error Name": "HEX_A"},
                {"Reporter ID": "0x8134", "Error Code": "0x200",
                 "Meta Data": "0x22A", "Error Name": "HEX_B"},
                {"Reporter ID": "0x8134", "Error Code": "0x200",
                 "Meta Data": 50, "Error Name": "DECIMAL"},
                {"Reporter ID": "0x8134", "Error Code": "0x200",
                 "Meta Data": "NA", "Error Name": "NONE"},
            ]}}), encoding="utf-8")
            build_error_reference(source, destination)
            json_reference = load_error_reference(source)
            database = load_error_reference(destination)
            try:
                base = {"reporter_id": "81 34", "error_code": "00 00 02 00"}
                for attribute, expected in [
                    ("00 00 02 2A", "HEX_B"),
                    ("00 00 00 32", "DECIMAL"),
                    ("00 00 09 99", "[1] HEX_A; [2] HEX_B; [3] DECIMAL; [4] NONE"),
                    ("", "[1] HEX_A; [2] HEX_B; [3] DECIMAL; [4] NONE"),
                ]:
                    record = {**base, "error_attribute": attribute}
                    with self.subTest(attribute=attribute):
                        self.assertEqual(
                            fe_03_reference_values(record, database)[0], expected
                        )
                        self.assertEqual(
                            fe_03_reference_values(record, database),
                            fe_03_reference_values(record, json_reference),
                        )
            finally:
                database.close()

    def test_bundled_database_has_descriptions_and_actions(self):
        database = load_error_reference(DEFAULT_REFERENCE)
        try:
            values = fe_03_reference_values({
                "reporter_id": "9F0D", "error_code": "0001",
            }, database)
            self.assertEqual(values[0], "MCUFOEM_SOC_ASSERTION_MISMATCH")
            self.assertIn("SOC pin", values[1])
            self.assertEqual(values[-2], "N/A")
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
