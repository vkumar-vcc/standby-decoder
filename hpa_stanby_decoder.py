import re
from datetime import datetime, UTC

INPUT_FILE = "hp_coldboot.log"
REQUEST = "1D12 224721"
FE_03_REQUEST = "1D0D 22FE03"

def construct_regex(request):
    return rf"^Tester\s*->\s*{re.escape(request)}\s*$.*?(?=^# Sending Request:|\Z)"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    coldboot_data = f.read()

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
    raise ValueError(f"Request '{REQUEST}' was not found in {INPUT_FILE}")
if fe_03_match is None:
    raise ValueError(f"Request '{FE_03_REQUEST}' was not found in {INPUT_FILE}")

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

widths = [
    max(len(str(value)) for value in column)
    for column in zip(headers, *rows)
]

def format_row(values):
    return "| " + " | ".join(
        f"{str(value):<{width}}" for value, width in zip(values, widths)
    ) + " |"

def format_separator():
    return "+-" + "-+-".join("-" * width for width in widths) + "-+"

print(format_separator())
print(format_row(headers))
print(format_separator())
for row in rows:
    print(format_row(row))
print(format_separator())

if fe_03_rows:
    fe_03_widths = [
        max(len(str(value)) for value in column)
        for column in zip(fe_03_headers, *fe_03_rows)
    ]

    def format_fe_03_row(values):
        return "| " + " | ".join(
            f"{str(value):<{width}}"
            for value, width in zip(values, fe_03_widths)
        ) + " |"

    def format_fe_03_separator():
        return "+-" + "-+-".join("-" * width for width in fe_03_widths) + "-+"

    print("\nFE03 error slots (non-empty):")
    print(format_fe_03_separator())
    print(format_fe_03_row(fe_03_headers))
    print(format_fe_03_separator())
    for row in fe_03_rows:
        print(format_fe_03_row(row))
    print(format_fe_03_separator())