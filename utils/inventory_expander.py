#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Dell Inc. or its subsidiaries. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared sparse-to-complete inventory expansion utility.

This module is used by idrac_watcher.py and the magellan_discovery Ansible role.
It expands a sparse admin_inventory.csv (SERVICE_TAG, GROUP_NAME,
FUNCTIONAL_GROUP_NAME, ROW, RACK, SLOT, RANGE) into a complete CSV containing
USLOT and BMC_IP values.

USLOT assignment is independent of IP assignment:
- USLOT is assigned per (GROUP_NAME, ROW, RACK) starting at 1.
- BMC_IP is assigned sequentially from the GROUP_NAME's RANGE.
"""

import argparse
import csv
import ipaddress
import os
import sys
from typing import Any, Dict, List, Tuple

REQUIRED_COLUMNS = [
    "SERVICE_TAG",
    "GROUP_NAME",
    "FUNCTIONAL_GROUP_NAME",
    "ROW",
    "RACK",
    "SLOT",
    "RANGE",
]

COMPLETED_INVENTORY_COLUMNS = [
    "SERVICE_TAG",
    "GROUP_NAME",
    "FUNCTIONAL_GROUP_NAME",
    "ROW",
    "RACK",
    "USLOT",
    "ROW_INT",
    "RACK_INT",
    "USLOT_INT",
    "BMC_IP",
]


def _ip_to_int(ip_str: str) -> int:
    """Convert an IPv4 address string to a 32-bit integer."""
    return int(ipaddress.IPv4Address(ip_str))


def _int_to_ip(ip_int: int) -> str:
    """Convert a 32-bit integer to an IPv4 address string."""
    return str(ipaddress.IPv4Address(ip_int))


def _is_valid_ipv4(ip_str: str) -> bool:
    """Return True if the string is a valid IPv4 address."""
    try:
        ipaddress.ip_address(ip_str)
        return True
    except ValueError:
        return False


def _is_non_negative_int(value: str) -> bool:
    """Return True if the value is a non-negative integer."""
    try:
        return int(value) >= 0
    except (ValueError, TypeError):
        return False


def _carry_forward(rows: List[Dict[str, Any]]) -> None:
    """Fill blank GROUP_NAME, FUNCTIONAL_GROUP_NAME, ROW, RACK, and RANGE values.

    GROUP_NAME, FUNCTIONAL_GROUP_NAME, ROW, and RACK are carried from the last
    non-empty value seen in the file.  RANGE is carried per GROUP_NAME so that a
    group keeps its own range and does not inherit another group's range.
    """
    carry_columns = ["GROUP_NAME", "FUNCTIONAL_GROUP_NAME", "ROW", "RACK"]
    last_values: Dict[str, str] = {col: "" for col in carry_columns}
    group_ranges: Dict[str, str] = {}

    for row in rows:
        for col in carry_columns:
            value = row.get(col, "").strip()
            if value:
                last_values[col] = value
            row[col] = last_values[col]

        group = row["GROUP_NAME"]
        range_val = row.get("RANGE", "").strip()
        if range_val:
            group_ranges[group] = range_val
            row["RANGE"] = range_val
        elif group in group_ranges:
            row["RANGE"] = group_ranges[group]


def parse_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse the sparse admin inventory CSV and return a list of row dicts."""
    if not os.path.exists(csv_path):
        raise ValueError(f"CSV file not found: {csv_path}")

    errors = []
    rows: List[Dict[str, Any]] = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV file is empty or has no header row")

        fieldnames = [name.strip() for name in reader.fieldnames]
        missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        raw_rows: List[Dict[str, Any]] = []
        for row_num, raw_row in enumerate(reader, start=2):
            row = {
                k.strip(): (v.strip() if v is not None else "")
                for k, v in raw_row.items()
            }
            row["_row_num"] = row_num
            raw_rows.append(row)

    _carry_forward(raw_rows)

    for row in raw_rows:
        row_num = row["_row_num"]

        for col in ("SERVICE_TAG", "GROUP_NAME", "FUNCTIONAL_GROUP_NAME", "ROW", "RACK", "RANGE"):
            if not row.get(col):
                errors.append(f"Row {row_num}: missing required value for {col}")

        if not row.get("ROW") or not _is_non_negative_int(row["ROW"]):
            errors.append(f"Row {row_num}: ROW must be a non-negative integer")
        if not row.get("RACK") or not _is_non_negative_int(row["RACK"]):
            errors.append(f"Row {row_num}: RACK must be a non-negative integer")

        if row.get("SLOT") and not _is_non_negative_int(row["SLOT"]):
            errors.append(f"Row {row_num}: SLOT must be empty or a non-negative integer")

        range_val = row.get("RANGE", "")
        if "-" not in range_val:
            errors.append(f"Row {row_num}: RANGE must be in 'start-end' format")
        else:
            parts = range_val.split("-")
            if len(parts) != 2 or not _is_valid_ipv4(parts[0]) or not _is_valid_ipv4(parts[1]):
                errors.append(f"Row {row_num}: RANGE contains invalid IPv4 addresses")

        if row.get("SERVICE_TAG"):
            row["ROW_INT"] = int(row["ROW"])
            row["RACK_INT"] = int(row["RACK"])
            rows.append(row)

    if errors:
        raise ValueError("\n".join(errors))

    return rows


def assign_uslots(rows: List[Dict[str, Any]]) -> None:
    """Assign USLOT values per (GROUP_NAME, ROW, RACK).

    Empty SLOT values are filled with the next available USLOT starting at 1.
    Provided SLOT values are checked for duplicates within the same group.
    """
    errors = []
    groups: Dict[Tuple[str, int, int], List[Dict[str, Any]]] = {}
    for row in rows:
        key = (row["GROUP_NAME"], row["ROW_INT"], row["RACK_INT"])
        groups.setdefault(key, []).append(row)

    for key, group_rows in groups.items():
        # Pre-collect provided SLOT values so auto-assigned slots never
        # collide with a SLOT provided on a later row in the same group.
        provided_slots: Dict[int, int] = {}
        for row in group_rows:
            s = row.get("SLOT", "").strip()
            if s:
                slot = int(s)
                provided_slots[slot] = provided_slots.get(slot, 0) + 1

        assigned = set()
        next_slot = 1
        for row in group_rows:
            s = row.get("SLOT", "").strip()
            row_num = row["_row_num"]
            if s:
                uslot_int = int(s)
                if uslot_int in assigned:
                    errors.append(
                        f"Row {row_num}: duplicate SLOT {uslot_int} in group {key}"
                    )
                elif provided_slots.get(uslot_int, 0) == 0:
                    errors.append(
                        f"Row {row_num}: duplicate SLOT {uslot_int} in group {key}"
                    )
                else:
                    provided_slots[uslot_int] -= 1
                    assigned.add(uslot_int)
            else:
                while next_slot in assigned or provided_slots.get(next_slot, 0) > 0:
                    next_slot += 1
                uslot_int = next_slot
                assigned.add(uslot_int)
                next_slot += 1

            row["USLOT"] = str(uslot_int)
            row["USLOT_INT"] = uslot_int

    if errors:
        raise ValueError("\n".join(errors))


def check_subnet_lengths(rows: List[Dict[str, Any]]) -> List[str]:
    """Return a list of error messages if a GROUP_NAME has too few IPs in its RANGE."""
    errors = []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["GROUP_NAME"], []).append(row)

    for group_name, group_rows in groups.items():
        ranges = set(r["RANGE"] for r in group_rows)
        if len(ranges) > 1:
            errors.append(
                f"Group {group_name}: all rows in a group must share the same RANGE"
            )
            continue

        start, end = group_rows[0]["RANGE"].split("-")
        start_int = _ip_to_int(start)
        end_int = _ip_to_int(end)
        if start_int > end_int:
            errors.append(
                f"Group {group_name}: RANGE start {start} is greater than end {end}"
            )
            continue

        available = end_int - start_int + 1
        if len(group_rows) > available:
            errors.append(
                f"Group {group_name}: {len(group_rows)} entries but RANGE {start}-{end} "
                f"only provides {available} IPs"
            )

    return errors


def allocate_ips(rows: List[Dict[str, Any]]) -> None:
    """Assign BMC_IP values sequentially from each GROUP_NAME's RANGE.

    Rows within a group are ordered by ROW, RACK, USLOT for deterministic IP allocation.
    """
    errors = []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["GROUP_NAME"], []).append(row)

    for group_name, group_rows in groups.items():
        ranges = set(r["RANGE"] for r in group_rows)
        if len(ranges) > 1:
            errors.append(
                f"Group {group_name}: all rows in a group must share the same RANGE"
            )
            continue

        start, end = group_rows[0]["RANGE"].split("-")
        start_int = _ip_to_int(start)
        end_int = _ip_to_int(end)
        if start_int > end_int:
            errors.append(
                f"Group {group_name}: RANGE start {start} is greater than end {end}"
            )
            continue

        if len(group_rows) > end_int - start_int + 1:
            errors.append(
                f"Group {group_name}: not enough IPs in RANGE {start}-{end}"
            )
            continue

        sorted_rows = sorted(group_rows, key=lambda r: (r["ROW_INT"], r["RACK_INT"], r["USLOT_INT"]))
        for idx, row in enumerate(sorted_rows):
            row["BMC_IP"] = _int_to_ip(start_int + idx)

    if errors:
        raise ValueError("\n".join(errors))


def build_complete(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the complete inventory list in the original CSV order."""
    seen_service_tags: Dict[str, int] = {}
    errors = []
    complete: List[Dict[str, Any]] = []

    for row in rows:
        service_tag = row["SERVICE_TAG"]
        row_num = row["_row_num"]
        if service_tag in seen_service_tags:
            errors.append(
                f"Row {row_num}: duplicate SERVICE_TAG '{service_tag}' "
                f"(first seen at row {seen_service_tags[service_tag]})"
            )
        else:
            seen_service_tags[service_tag] = row_num

        complete.append({
            "SERVICE_TAG": service_tag,
            "GROUP_NAME": row["GROUP_NAME"],
            "FUNCTIONAL_GROUP_NAME": row["FUNCTIONAL_GROUP_NAME"],
            "ROW": str(row["ROW_INT"]),
            "RACK": str(row["RACK_INT"]),
            "USLOT": str(row["USLOT_INT"]),
            "ROW_INT": row["ROW_INT"],
            "RACK_INT": row["RACK_INT"],
            "USLOT_INT": row["USLOT_INT"],
            "BMC_IP": row["BMC_IP"],
        })

    if errors:
        raise ValueError("\n".join(errors))

    if not complete:
        raise ValueError("CSV file has no data rows")

    return complete


def expand_inventory(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expand already-parsed sparse rows into complete inventory rows."""
    assign_uslots(rows)
    errors = check_subnet_lengths(rows)
    if errors:
        raise ValueError("\n".join(errors))
    allocate_ips(rows)
    return build_complete(rows)


def load_sparse_inventory(csv_path: str) -> List[Dict[str, Any]]:
    """Parse the sparse admin inventory CSV and expand it to a complete list."""
    rows = parse_csv(csv_path)
    return expand_inventory(rows)


def save_complete_inventory_csv(path: str, complete: List[Dict[str, Any]]) -> None:
    """Write the complete inventory to a CSV file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COMPLETED_INVENTORY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(complete)


def main() -> int:
    """CLI entry point for the inventory expander."""
    parser = argparse.ArgumentParser(description="Expand sparse admin_inventory.csv to a complete inventory CSV")
    parser.add_argument("--input", required=True, help="Path to sparse admin_inventory.csv")
    parser.add_argument("--csv", required=False, help="Path to write the complete inventory CSV")
    parser.add_argument("--validate", action="store_true", help="Validate the sparse CSV without writing output")
    args = parser.parse_args()

    complete = load_sparse_inventory(args.input)

    if args.validate:
        print(f"Validation passed: {len(complete)} entries")
        return 0

    if not args.csv:
        print("ERROR: --csv is required unless --validate is used", file=sys.stderr)
        return 1

    save_complete_inventory_csv(args.csv, complete)
    print(f"Complete inventory written to {args.csv} ({len(complete)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
