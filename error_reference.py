import argparse
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


DEFAULT_REFERENCE = Path(__file__).with_name("error_reference.sqlite3")
FE_03_REFERENCE_FIELDS = [
    ("Error Name", "errorname"),
    ("Description", "description"),
    ("Error Level", "errorlevel"),
    ("Expected SOC_ERROR", "assertssocerror"),
    ("HI Action", "hi"),
    ("HP Action", "hp"),
]
REFERENCE_COLUMNS = ", ".join(field for _, field in FE_03_REFERENCE_FIELDS)


def reference_identifier(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= 0xFFFFFFFF else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{1,8}", text):
        return int(text, 16)
    if re.fullmatch(r"[0-9a-fA-F]{2}(?:\s+[0-9a-fA-F]{2}){1,3}", text):
        return int("".join(text.split()), 16)
    return None


def meta_data_identifier(value):
    # Workbook meta data is hex when prefixed with 0x, otherwise decimal.
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 <= value <= 0xFFFFFFFF else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,8}", text):
        return int(text, 16)
    if re.fullmatch(r"[0-9]{1,10}", text) and int(text) <= 0xFFFFFFFF:
        return int(text)
    return None


def reference_field(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def error_description(fields):
    description = fields.get("description")
    if description is not None and str(description).strip():
        return str(description)
    mechanism = fields.get("safetymechanismname")
    if mechanism is not None and str(mechanism).strip():
        return f"Safety mechanism: {mechanism}"
    return "N/A"


def load_json_reference(path):
    with open(path, encoding="utf-8-sig") as source:
        sheets = json.load(source)
    if not isinstance(sheets, dict):
        raise ValueError("Error reference must be an object keyed by worksheet name")
    index = {}
    for sheet_name, sheet in sheets.items():
        if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
            raise ValueError(f"Invalid rows in reference worksheet {sheet_name!r}")
        row_numbers = sheet.get("row_numbers")
        if row_numbers is not None and (
            not isinstance(row_numbers, list)
            or len(row_numbers) != len(sheet["rows"])
            or any(type(number) is not int or number < 1 for number in row_numbers)
        ):
            raise ValueError(f"Invalid row numbers in reference worksheet {sheet_name!r}")
        column_headers = {}
        for position, row in enumerate(sheet["rows"]):
            if not isinstance(row, dict):
                raise ValueError(f"Invalid row in reference worksheet {sheet_name!r}")
            # Some exported sheets retain column letters because their headers vary.
            if row and all(re.fullmatch(r"[A-Z]+", key) for key in row):
                labels = {
                    reference_field(value)
                    for value in row.values() if isinstance(value, str)
                }
                if "reporterid" in labels and "errorname" in labels:
                    column_headers = {key: str(value) for key, value in row.items()}
                    continue
                row = {column_headers.get(key, key): value for key, value in row.items()}
            fields = {reference_field(key): value for key, value in row.items()}
            reporter = reference_identifier(fields.get("reporterid"))
            code = reference_identifier(fields.get("errorcode"))
            if reporter is None or code is None:
                continue
            fields["description"] = error_description(fields)
            source_row = row_numbers[position] if row_numbers is not None else None
            location = f"{sheet_name}:{source_row}" if source_row else sheet_name
            index.setdefault((reporter, code), []).append(
                (location, meta_data_identifier(fields.get("metadata")), fields)
            )
    if not index:
        raise ValueError("Error reference contains no exact Reporter ID / Error Code entries")
    return index


def load_error_reference(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        return load_json_reference(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError(
                "Unsupported error reference database version; rebuild it with "
                "error_reference.py"
            )
        connection.execute(
            f"SELECT reference, meta_data, {REFERENCE_COLUMNS} FROM errors LIMIT 0"
        )
        if connection.execute("SELECT 1 FROM errors LIMIT 1").fetchone() is None:
            raise ValueError("Error reference database contains no entries")
    except (sqlite3.Error, ValueError):
        connection.close()
        raise
    return connection


def fe_03_reference_values(record, index):
    key = (
        reference_identifier(record.get("reporter_id")),
        reference_identifier(record.get("error_code")),
    )
    if isinstance(index, sqlite3.Connection):
        matches = [
            (row[0], row[1], dict(zip(
                (field for _, field in FE_03_REFERENCE_FIELDS), row[2:]
            )))
            for row in index.execute(
                f"SELECT reference, meta_data, {REFERENCE_COLUMNS} FROM errors "
                "WHERE reporter_id = ? AND error_code = ? ORDER BY entry_id",
                key,
            )
        ]
    else:
        matches = index.get(key, [])
    if not matches:
        return ["Not found", *["N/A"] * (len(FE_03_REFERENCE_FIELDS) - 1), "No exact match"]
    # The FE03 error attribute carries the workbook meta data, which identifies
    # the exact error when several entries share a Reporter ID / Error Code.
    attribute = reference_identifier(record.get("error_attribute"))
    if attribute is not None:
        narrowed = [match for match in matches if match[1] == attribute]
        if narrowed:
            matches = narrowed
    matches = [(location, fields) for location, _, fields in matches]
    values = []
    for _, field in FE_03_REFERENCE_FIELDS:
        alternatives = []
        for number, (_, fields) in enumerate(matches, 1):
            text = " ".join(str(fields.get(field, "N/A")).split()) or "N/A"
            alternatives.append(f"[{number}] {text}" if len(matches) > 1 else text)
        values.append("; ".join(alternatives))
    values.append("; ".join(
        f"[{number}] {location}" if len(matches) > 1 else location
        for number, (location, _) in enumerate(matches, 1)
    ))
    return values


def build_error_reference(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Source JSON and destination database must be different files")
    index = load_json_reference(source)
    entries = [
        (
            reporter, code, meta_data, location,
            *(str(fields.get(field, "N/A")) for _, field in FE_03_REFERENCE_FIELDS),
        )
        for (reporter, code), matches in index.items()
        for location, meta_data, fields in matches
    ]
    with tempfile.NamedTemporaryFile(
        prefix=destination.stem + "-", suffix=".sqlite3",
        dir=destination.parent, delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with closing(sqlite3.connect(temporary_path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE errors (entry_id INTEGER PRIMARY KEY, "
                    "reporter_id INTEGER NOT NULL, error_code INTEGER NOT NULL, "
                    "meta_data INTEGER, reference TEXT NOT NULL, "
                    + ", ".join(
                        f"{field} TEXT NOT NULL" for _, field in FE_03_REFERENCE_FIELDS
                    ) + ")"
                )
                connection.executemany(
                    f"INSERT INTO errors (reporter_id, error_code, meta_data, reference, "
                    f"{REFERENCE_COLUMNS}) VALUES ("
                    + ", ".join("?" for _ in range(4 + len(FE_03_REFERENCE_FIELDS)))
                    + ")",
                    entries,
                )
                connection.execute(
                    "CREATE INDEX error_pair ON errors (reporter_id, error_code)"
                )
                connection.execute("PRAGMA user_version = 2")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Generated error reference failed SQLite integrity check")
            stored = connection.execute(
                f"SELECT reporter_id, error_code, meta_data, reference, {REFERENCE_COLUMNS} "
                "FROM errors ORDER BY entry_id"
            ).fetchall()
            if stored != entries:
                raise ValueError("Generated error reference does not match source entries")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return len(entries), len(index)


def main():
    parser = argparse.ArgumentParser(
        description="Build a compact, indexed FE03 SQLite reference from workbook JSON."
    )
    parser.add_argument("source", type=Path, help="Converted workbook JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_REFERENCE)
    args = parser.parse_args()
    try:
        entries, pairs = build_error_reference(args.source, args.output)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(f"Stored {entries} entries / {pairs} exact identifier pairs in {args.output}")
    print(f"Size: {args.output.stat().st_size:,} bytes")
    print("Verified every stored field against the source; SQLite integrity check passed.")


if __name__ == "__main__":
    main()
