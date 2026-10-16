import argparse
import re
from datetime import datetime, timedelta, UTC

REQUEST = "1D12 224721"
FE_03_REQUEST = "1D0D 22FE03"
VEHICLE_TIME_REQUEST = "1D12 22DD0E"
TRANSITION_REQUEST = "1D12 22D991"

def construct_regex(request):
    address, service = request.split()
    service_pattern = r"\s*".join(
        re.escape(service[i:i + 2]) for i in range(0, len(service), 2)
    )
    return rf"^Tester\s*->\s*{address}\s+{service_pattern}\s*$.*?(?=^# Sending Request:|\Z)"


def response_payload(block, did):
    match = re.search(
        r"^Complete Response:\s*1D12\s+([0-9A-Fa-f ]+)\s*$",
        block,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    response = bytes.fromhex(match.group(1))
    prefix = bytes.fromhex(f"62{did}")
    if not response.startswith(prefix):
        raise ValueError(f"Expected a positive response for DID {did}")
    return response[len(prefix):]


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
    return [
        [
            index,
            states.get(state, f"Unknown ({state})"),
            (reference_utc + timedelta(milliseconds=timestamp - anchors[0]))
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ]
        for index, state, timestamp in entries
    ]

parser = argparse.ArgumentParser(description="Decode standby events from a coldboot log.")
parser.add_argument(
    "--input-file",
    default="hp_coldboot.log",
    help="Path to the coldboot log (default: hp_coldboot.log)",
)
args = parser.parse_args()
input_file = args.input_file

with open(input_file, "r", encoding="utf-8") as f:
    coldboot_data = f.read()

transition_rows = decode_transition_rows(coldboot_data)

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

    m = re.match(r"Shutdown Type (\d+): .*?Sleep", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["shutdown_type"] = "Sleep"
        continue

    m = re.match(r"VcuShutdownIntent from VMM (\d+): .*?StandbyWithInSystemTestRequested", line)
    if m:
        idx = int(m.group(1))
        records.setdefault(idx, {})["intent"] = "StandbyWithInSystemTestRequested"
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

fe_03_headers = [
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
                *error_values[1:],
            ]
        )

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


print_table(headers, rows)

if fe_03_rows:
    print("\nFE03 error slots (non-empty):")
    print_table(fe_03_headers, fe_03_rows)

if transition_rows:
    print("\nPlatform state transition history (UTC):")
    print_table(["ID", "State", "Timestamp (UTC)"], transition_rows)