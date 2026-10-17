import argparse
import re
import shutil
import sqlite3
import textwrap
from datetime import datetime, timedelta, UTC
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from error_reference import (
    DEFAULT_REFERENCE,
    FE_03_REFERENCE_FIELDS,
    fe_03_reference_values,
    load_error_reference,
)

REQUEST = "1D12 224721"
FE_03_REQUEST = "1D0D 22FE03"
VEHICLE_TIME_REQUEST = "1D12 22DD0E"
TRANSITION_REQUEST = "1D12 22D991"
WATCHDOG_REQUEST = "1D12 22D99D"
DTC_ECUS = [
    ("HIA", "1D0D"),
    ("HIB", "1D0E"),
    ("HPA", "1D12"),
]

def construct_regex(request):
    address, service = request.split()
    service_pattern = r"\s*".join(
        re.escape(service[i:i + 2]) for i in range(0, len(service), 2)
    )
    return rf"^Tester\s*->\s*{address}\s+{service_pattern}\s*$.*?(?=^# Sending Request:|\Z)"


def complete_response_bytes(block, address):
    match = re.search(
        rf"^Complete Response:\s*{re.escape(address)}\s+([0-9A-Fa-f ]+)\s*$",
        block,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    return bytes.fromhex(match.group(1))


def response_payload(block, did):
    response = complete_response_bytes(block, "1D12")
    if response is None:
        return None
    prefix = bytes.fromhex(f"62{did}")
    if not response.startswith(prefix):
        raise ValueError(f"Expected a positive response for DID {did}")
    return response[len(prefix):]


def decode_watchdog_rows(log):
    match = re.search(
        construct_regex(WATCHDOG_REQUEST), log, re.MULTILINE | re.DOTALL
    )
    if match is None:
        return None, None
    block = match.group(0)
    payload = response_payload(block, "D99D")
    if payload is not None:
        if len(payload) < 2:
            raise ValueError("Watchdog response is missing version and data health")
        if payload[0] != 1:
            raise ValueError(f"Unsupported watchdog data structure version: {payload[0]}")
        if payload[1] == 3:
            return None, "Watchdog timeline unavailable: Data Health 3 (No data available)."
        if payload[1] != 0:
            raise ValueError(f"Watchdog response reports data health error: {payload[1]}")
        timeline = payload[2:].rstrip(b"\x00").decode("ascii")
    else:
        health = re.search(r"^Data Health:\s*(\d+)", block, re.MULTILINE)
        if health and int(health.group(1)) == 3:
            return None, "Watchdog timeline unavailable: Data Health 3 (No data available)."
        if health and int(health.group(1)) != 0:
            raise ValueError(
                f"Watchdog response reports data health error: {health.group(1)}"
            )
        marker = "Application name and timeline:"
        if marker not in block:
            raise ValueError("Watchdog response is missing its application timeline")
        timeline = block.split(marker, 1)[1]

    rows = []
    for line in timeline.splitlines():
        if not line.strip():
            continue
        application, separator, timestamp = line.strip().partition(",")
        if not separator or not application or not timestamp:
            raise ValueError(f"Invalid watchdog timeline entry: {line!r}")
        datetime.fromisoformat(timestamp)
        rows.append([len(rows) + 1, application, timestamp])
    return rows, None


def summarize_watchdog_rows(rows):
    applications = {}
    for _, application, timestamp in rows:
        applications.setdefault(application, []).append(timestamp)
    summary = []
    for application, timestamps in applications.items():
        valid = [timestamp for timestamp in timestamps if not timestamp.startswith("1970-")]
        summary.append([
            application,
            len(timestamps),
            min(valid) if valid else "N/A",
            max(valid) if valid else "N/A",
            len(timestamps) - len(valid),
        ])
    return sorted(summary, key=lambda row: (-row[1], row[0]))


def format_elapsed(milliseconds):
    sign = "-" if milliseconds < 0 else ""
    seconds, milliseconds = divmod(abs(milliseconds), 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def decode_transition_rows(log):
    history = re.search(
        construct_regex(TRANSITION_REQUEST), log, re.MULTILINE | re.DOTALL
    )
    if history is None:
        return []
    vehicle_time = re.search(
        construct_regex(VEHICLE_TIME_REQUEST), log, re.MULTILINE | re.DOTALL
    )
    if vehicle_time is None:
        raise ValueError("Platform state history requires a Vehicle Time (22DD0E) request")

    utc_match = re.search(
        r"^Coordinated Universal Time \(UTC\):\s*(\S+)",
        vehicle_time.group(0),
        re.MULTILINE,
    )
    if utc_match:
        utc_text = utc_match.group(1)
    else:
        payload = response_payload(vehicle_time.group(0), "DD0E")
        if payload is None or len(payload) <= 4:
            raise ValueError("Vehicle Time response is missing its UTC value")
        utc_text = payload[4:].decode("ascii")
    reference_utc = datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
    if reference_utc.utcoffset() != timedelta(0):
        raise ValueError("Vehicle Time must contain an explicit UTC timestamp")

    states = {
        1: "Run",
        2: "Standby",
        7: "PrepareForStandby",
        8: "PrepareForResume",
        9: "Resume",
        255: "Now",
    }
    payload = response_payload(history.group(0), "D991")
    entries = []
    if payload is not None:
        if not payload or len(payload) % 9:
            raise ValueError("Platform state history must contain 9-byte records")
        entries = [
            (i // 9 + 1, payload[i], int.from_bytes(payload[i + 1:i + 9], "big"))
            for i in range(0, len(payload), 9)
        ]
    else:
        state_matches = re.findall(
            r"^State ID (\d+):\s*(\d+).*$", history.group(0), re.MULTILINE
        )
        timestamps = dict(re.findall(
            r"^Timestamp (\d+):\s*(\d+)\s+ms\b", history.group(0), re.MULTILINE
        ))
        for index, state in state_matches:
            if index not in timestamps:
                raise ValueError(f"Missing timestamp for platform state entry {index}")
            entries.append((int(index), int(state), int(timestamps[index])))

    anchors = [timestamp for _, state, timestamp in entries if state == 255]
    if len(anchors) != 1:
        raise ValueError("Platform state history must contain exactly one Now (255) entry")
    now_position = next(
        position for position, (_, state, _) in enumerate(entries) if state == 255
    )
    entries = entries[:now_position + 1]
    return [
        [
            index,
            states.get(state, f"Unknown ({state})"),
            (reference_utc + timedelta(milliseconds=timestamp - anchors[0]))
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            format_elapsed(timestamp - entries[position - 1][2])
            if position else "N/A",
        ]
        for position, (index, state, timestamp) in enumerate(entries)
    ]


def decode_dtc_rows(log, address, reference_global_time, reference_utc):
    list_request = f"{address} 1903"
    listing = re.search(
        construct_regex(list_request), log, re.MULTILINE | re.DOTALL
    )
    if listing is None:
        return None

    response = complete_response_bytes(listing.group(0), address)
    if response is None or not response.startswith(b"\x59\x03"):
        raise ValueError(f"{address} DTC list is missing its positive 19 03 response")
    payload = response[2:]
    if len(payload) % 4:
        raise ValueError(f"{address} DTC list must contain 4-byte DTC/status records")

    descriptions = {
        code.upper(): description.strip()
        for code, description in re.findall(
            r"^\s*([0-9A-Fa-f]{6})\s+(.+?)\s+\d+\s+Snapshot Datarecord\s*$",
            listing.group(0),
            re.MULTILINE,
        )
    }
    dtcs = list(dict.fromkeys(
        payload[index:index + 3].hex().upper()
        for index in range(0, len(payload), 4)
    ))

    rows = []
    for dtc in dtcs:
        detail_pattern = construct_regex(f"{address} 1904{dtc}FF")
        detail_blocks = [
            match.group(0)
            for match in re.finditer(
                detail_pattern, log, re.MULTILINE | re.DOTALL
            )
        ]
        for block in detail_blocks:
            response = complete_response_bytes(block, address)
            if response is not None and not response.startswith(
                bytes.fromhex(f"5904{dtc}")
            ):
                raise ValueError(
                    f"Expected a positive 19 04 response for {address} DTC {dtc}"
                )
        global_times = [
            (Decimal(match.group(1)), match.group(1))
            for block in detail_blocks
            for match in re.finditer(
                r"^[ \t]*Global Real Time:\s*(\d+(?:\.\d+)?)\s*min\b",
                block,
                re.MULTILINE,
            )
        ]
        latest_global_time = max(global_times, default=None)
        if latest_global_time is not None and (
            reference_global_time is None or reference_utc is None
        ):
            raise ValueError(
                f"{address} DTC timestamps require Global Real Time and UTC in the "
                "Vehicle Time (22DD0E) response"
            )
        utc_time = (
            (
                reference_utc
                + timedelta(milliseconds=int(
                    ((latest_global_time[0] - reference_global_time) * 60000)
                    .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                ))
            )
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if latest_global_time is not None else "N/A"
        )
        events = [
            match.group(1).strip()
            for block in detail_blocks
            for match in re.finditer(
                rf"^[ \t]*Latest event which caused {re.escape(dtc)}:\s*(.*)$",
                block,
                re.MULTILINE | re.IGNORECASE,
            )
        ]
        rows.append([
            dtc,
            descriptions.get(dtc, "N/A"),
            f"{latest_global_time[1]} min"
            if latest_global_time is not None else "N/A",
            utc_time,
            events[-1] if events else "N/A",
        ])
    return rows


def decode_vehicle_time_anchor(log):
    vehicle_time = re.search(
        construct_regex(VEHICLE_TIME_REQUEST), log, re.MULTILINE | re.DOTALL
    )
    if vehicle_time is None:
        return None, None
    block = vehicle_time.group(0)
    global_match = re.search(
        r"^[ \t]*Global Real Time:\s*(\d+(?:\.\d+)?)\s*min\b",
        block,
        re.MULTILINE,
    )
    utc_match = re.search(
        r"^[ \t]*Coordinated Universal Time \(UTC\):\s*(\S+)",
        block,
        re.MULTILINE,
    )
    if global_match is None or utc_match is None:
        return None, None
    reference_utc = datetime.fromisoformat(
        utc_match.group(1).replace("Z", "+00:00")
    )
    if reference_utc.utcoffset() != timedelta(0):
        raise ValueError("Vehicle Time must contain an explicit UTC timestamp")
    return Decimal(global_match.group(1)), reference_utc


def decode_ecu_dtc_rows(log):
    reference_global_time, reference_utc = decode_vehicle_time_anchor(log)
    return {
        ecu: rows
        for ecu, address in DTC_ECUS
        if (rows := decode_dtc_rows(
            log, address, reference_global_time, reference_utc
        )) is not None
    }


parser = argparse.ArgumentParser(description="Decode standby events from a coldboot log.")
parser.add_argument(
    "--input-file",
    default="hp_coldboot.log",
    help="Path to the coldboot log (default: hp_coldboot.log)",
)
parser.add_argument(
    "--watchdog-details",
    action="store_true",
    help="Also print the full watchdog failure timeline",
)
parser.add_argument(
    "--error-reference",
    type=Path,
    default=DEFAULT_REFERENCE,
    help="FE03 SQLite or workbook JSON reference (default: bundled error_reference.sqlite3)",
)
parser.add_argument(
    "--no-error-reference",
    action="store_true",
    help="Print the original FE03 columns without reference enrichment",
)
args = parser.parse_args()
input_file = args.input_file

error_reference = None
if not args.no_error_reference:
    try:
        error_reference = load_error_reference(args.error_reference)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(f"Cannot load error reference {args.error_reference}: {error}")

with open(input_file, "r", encoding="utf-8") as f:
    coldboot_data = f.read()

transition_rows = decode_transition_rows(coldboot_data)
watchdog_rows, watchdog_status = decode_watchdog_rows(coldboot_data)
ecu_dtc_rows = decode_ecu_dtc_rows(coldboot_data)

request_match = re.search(
        construct_regex(REQUEST),
        coldboot_data,
        flags=re.MULTILINE | re.DOTALL,
)

fe_03_match = re.search(
        construct_regex(FE_03_REQUEST),
        coldboot_data,
        flags=re.MULTILINE | re.DOTALL,
)

if request_match is None:
    raise ValueError(f"Request '{REQUEST}' was not found in {input_file}")
if fe_03_match is None:
    raise ValueError(f"Request '{FE_03_REQUEST}' was not found in {input_file}")

data = request_match.group(0)

records = {}

for line in data.splitlines():
    m = re.match(r"Timestamp when shutdown was requested to ShutdownManagerHIA (\d+): (\d+)", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["timestamp"] = int(m.group(2))
        continue

    m = re.match(r"Shutdown Type (\d+):\s*(?:\d+\s+)?(\S.*?)\s*$", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["shutdown_type"] = m.group(2)
        continue

    m = re.match(r"VcuShutdownIntent from VMM (\d+):\s*(?:\d+\s+)?(\S.*?)\s*$", line, re.IGNORECASE)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["intent"] = m.group(2)
        continue

    m = re.match(r"ISTWakeUpTimer (\d+): (\d+)", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["timer"] = int(m.group(2))
        continue

    m = re.match(r"ECUs still active/requested at forced shutdown (\d+): (.*)", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["ecus"] = m.group(2).rstrip(",")

    m = re.match(r"Flag indicating shutdown not successful (\d+): .*?(True|False)", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["success"] = "No" if m.group(2) == "True" else "Yes"

fe_03_records = {}

for line in fe_03_match.group(0).splitlines():
    patterns = [
        (r"Time of error in Error slot (\d+):\s*(.*)", "time"),
        (r"Error slot (\d+) - Reporter ID(?: \d+)?:\s*(.*)", "reporter_id"),
        (r"Error slot (\d+) - Error code:\s*(.*)", "error_code"),
        (r"Error slot (\d+) - Error attribute:\s*(.*)", "error_attribute"),
        (r"Error slot (\d+) - SOC_ERROR state:\s*(.*)", "soc_error"),
        (r"Error slot (\d+) - HP operation state:\s*(.*)", "operation_state"),
        (r"Error slot (\d+) - Occurrence of Error code:\s*(.*)", "occurrence"),
    ]
    for pattern, field in patterns:
        match = re.match(pattern, line)
        if match:
            slot = int(match.group(1))
            fe_03_records.setdefault(slot, {})[field] = match.group(2).strip()
            break

FE_03_SLOT_HEADERS = [
    "Slot",
    "Time",
    "Standby #",
    "Reporter ID",
    "Error Code",
    "Error Attribute",
    "SOC_ERROR",
    "HP Operation State",
    "Occurrence",
]
fe_03_headers = list(FE_03_SLOT_HEADERS)
if error_reference is not None:
    fe_03_headers.extend(header for header, _ in FE_03_REFERENCE_FIELDS)
    fe_03_headers.append("Reference")
fe_03_rows = []
default_values = {"", "00", "00 00", "00 00 00 00", "0", "0 False", "1970-01-01T00:00:00"}

standby_times = {
    idx: datetime.fromtimestamp(record["timestamp"], UTC)
    for idx, record in records.items()
    if "timestamp" in record
}

def correlate_fe_03_slot(timestamp):
    if not timestamp or timestamp in {"", "1970-01-01T00:00:00"}:
        return "No timestamp"

    error_time = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
    matching_standby = [
        idx for idx, standby_time in standby_times.items()
        if standby_time <= error_time
    ]
    if not matching_standby:
        return "Before #1"
    return str(max(matching_standby))

def compact_hex(value):
    # Show byte strings such as "00 00 02 2A" as workbook-style hex ("0x22A").
    if re.fullmatch(r"[0-9a-fA-F]{2}(?:\s+[0-9a-fA-F]{2})*", value.strip()):
        return f"0x{int(''.join(value.split()), 16):X}"
    return value

for slot in sorted(fe_03_records):
    record = fe_03_records[slot]
    error_values = [
        record.get("time", ""),
        record.get("reporter_id", ""),
        record.get("error_code", ""),
        record.get("error_attribute", ""),
        record.get("soc_error", ""),
        record.get("operation_state", ""),
        record.get("occurrence", ""),
    ]
    if any(value not in default_values for value in error_values):
        fe_03_rows.append(
            [
                slot,
                error_values[0],
                correlate_fe_03_slot(error_values[0]),
                *(compact_hex(value) for value in error_values[1:4]),
                *error_values[4:],
            ]
        )
        if error_reference is not None:
            fe_03_rows[-1].extend(fe_03_reference_values(record, error_reference))

if isinstance(error_reference, sqlite3.Connection):
    error_reference.close()

headers = [
    "#",
    "Timestamp",
    "UTC Time",
    "Shutdown Type",
    "VCU Intent",
    "IST Timer (HH:MM:SS)",
    "Active ECUs",
    "Success",
]
rows = []

def format_duration(seconds):
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

for idx in sorted(records):
    r = records[idx]
    utc = datetime.fromtimestamp(r["timestamp"], UTC).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    rows.append(
        [
            idx,
            r["timestamp"],
            utc,
            r.get("shutdown_type", ""),
            r.get("intent", ""),
            format_duration(r["timer"]) if "timer" in r else "",
            r.get("ecus") or "None",
            r.get("success", ""),
        ]
    )

def print_table(table_headers, table_rows):
    widths = [
        max(len(str(value)) for value in column)
        for column in zip(table_headers, *table_rows)
    ]

    def format_row(values):
        return "| " + " | ".join(
            f"{str(value):<{width}}"
            for value, width in zip(values, widths)
        ) + " |"

    separator = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    print(separator)
    print(format_row(table_headers))
    print(separator)
    for row in table_rows:
        print(format_row(row))
    print(separator)


def print_wrapped_table(table_headers, table_rows, total_width):
    def longest_word(value):
        return max((len(word) for word in str(value).split()), default=0)

    widths, minimums = [], []
    for position, header in enumerate(table_headers):
        values = [str(row[position]) for row in table_rows]
        widths.append(max([len(header)] + [len(value) for value in values]))
        # Prefer to keep header words, identifiers, and timestamps intact.
        minimums.append(min(40, max(longest_word(v) for v in [header, *values])))
    available = total_width - (3 * len(widths) + 1)
    # Shrink to whole words first; split words only if the table still does not fit.
    for limits in (minimums, [min(6, width) for width in widths]):
        while sum(widths) > available:
            shrinkable = [
                position for position, width in enumerate(widths)
                if width > limits[position]
            ]
            if not shrinkable:
                break
            widest = max(shrinkable, key=lambda position: widths[position])
            widths[widest] -= 1

    def wrap_row(values):
        return [
            textwrap.wrap(str(value), width=width, break_on_hyphens=False) or [""]
            for value, width in zip(values, widths, strict=True)
        ]

    def print_row(cells):
        for line in range(max(len(cell) for cell in cells)):
            print("| " + " | ".join(
                f"{(cell[line] if line < len(cell) else ''):<{width}}"
                for cell, width in zip(cells, widths)
            ) + " |")

    wrapped_rows = [wrap_row(row) for row in table_rows]
    # Divide rows only when a row spans several lines; otherwise keep it compact.
    divide_rows = any(len(cell) > 1 for cells in wrapped_rows for cell in cells)
    separator = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    print(separator)
    print_row(wrap_row(table_headers))
    print(separator)
    for position, cells in enumerate(wrapped_rows):
        print_row(cells)
        if divide_rows and position < len(wrapped_rows) - 1:
            print(separator)
    print(separator)


print_table(headers, rows)

if fe_03_rows:
    # Leave one spare column so the console does not auto-wrap full-width lines.
    output_width = max(80, shutil.get_terminal_size(fallback=(160, 24)).columns - 1)
    slot_columns = len(FE_03_SLOT_HEADERS) + (1 if error_reference is not None else 0)
    print("\nFE03 error slots (non-empty):")
    print_wrapped_table(
        fe_03_headers[:slot_columns],
        [row[:slot_columns] for row in fe_03_rows],
        output_width,
    )
    if error_reference is not None:
        print("\nFE03 error reference details:")
        print(textwrap.fill(f"Error reference: {args.error_reference}", width=output_width))
        print(textwrap.fill(
            "Exact Reporter ID + Error Code matches, narrowed by Error Attribute (workbook "
            "Meta Data) when it matches; template and bit-field rules are not inferred. "
            "[n] links alternatives to their references.",
            width=output_width,
        ))
        print_wrapped_table(
            ["Slot", *fe_03_headers[slot_columns:]],
            [[row[0], *row[slot_columns:]] for row in fe_03_rows],
            output_width,
        )

if transition_rows:
    print("\nPlatform state transition history (UTC):")
    print_table(
        ["ID", "State", "Timestamp (UTC)", "Time taken (HH:MM:SS.mmm)"],
        transition_rows,
    )

for ecu, dtc_rows in ecu_dtc_rows.items():
    print(f"\n{ecu} DTC snapshot summary:")
    if dtc_rows:
        print_table(
            ["DTC", "Description", "Latest Global Real Time", "UTC Time", "Latest Event"],
            dtc_rows,
        )
    else:
        print(f"No {ecu} DTCs recorded.")

if watchdog_status:
    print(f"\n{watchdog_status}")

if watchdog_rows is not None:
    print("\nHPA application watchdog failure summary:")
    print("Timestamps are as recorded; timezone is not specified by this readout.")
    summary = summarize_watchdog_rows(watchdog_rows)
    print(
        f"Total: {len(watchdog_rows)} failure entries across {len(summary)} applications."
    )
    print_table(
        ["Application", "Failures", "First valid timestamp", "Latest valid timestamp",
         "1970 timestamps"],
        summary,
    )
    if not watchdog_rows:
        print("No watchdog failure entries recorded.")
    if any(row[2].startswith("1970-") for row in watchdog_rows):
        print("1970 timestamps may indicate an unset clock; do not correlate them to UTC.")
    if args.watchdog_details:
        print("\nHPA application watchdog failure timeline:")
        print_table(["ID", "Application", "Timestamp (recorded)"], watchdog_rows)