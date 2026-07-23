#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automate iDRAC BMC network setup for Omnia/OpenCHAMI bare-metal provisioning.

This script ingests an admin inventory CSV, starts a temporary dnsmasq DHCP
server, matches iDRACs by BMC MAC address, and burns static IPs and physical
location metadata via Redfish.

It is the first step in the provisioning pipeline and runs BEFORE Omnia's
discovery/provision playbooks.
"""

import argparse
import csv
import io
import ipaddress
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package is required. Install it with: pip install requests")
    sys.exit(1)

try:
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning

    urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    try:
        from requests.packages.urllib3.exceptions import InsecureRequestWarning

        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    except ImportError:
        pass


# Constants

REDFISH_SYSTEM_ENDPOINT = "/redfish/v1/Systems/System.Embedded.1"
REDFISH_MANAGER_ATTRIBUTES_ENDPOINT = "/redfish/v1/Managers/iDRAC.Embedded.1/Attributes"
REDFISH_DEFAULT_PORT = 443
SYSTEM_EMBEDDED_ENDPOINT = "/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/DellAttributes/System.Embedded.1"

REQUIRED_COLUMNS = [
    "SERVICE_TAG",
    "ADMIN_MAC",
    "ADMIN_IP",
    "BMC_MAC",
    "BMC_IP",
    "HOSTNAME",
    "FUNCTIONAL_GROUP_NAME",
    "GROUP_NAME",
    "ROW",
    "RACK",
    "USLOT",
]
OPTIONAL_COLUMNS = ["PARENT_SERVICE_TAG", "IB_MAC", "IB_IP"]

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
HOSTNAME_RE = re.compile(
    r"^(([a-z]|[a-z][a-z0-9\-]*[a-z0-9])\.)*([a-z]|[a-z][a-z0-9\-]*[a-z0-9])$"
)

log = logging.getLogger("idrac_ip_config")
shutdown_requested = threading.Event()
_dnsmasq_process: Optional[subprocess.Popen] = None
_signal_count = 0


# Signal handlers

def _signal_handler(signum: int, frame: Any) -> None:
    """Handle SIGINT/SIGTERM by requesting graceful shutdown."""
    global _signal_count
    _signal_count += 1

    if _signal_count == 1:
        log.info("Received signal %d, requesting graceful shutdown", signum)
        shutdown_requested.set()
        if _dnsmasq_process is not None and _dnsmasq_process.poll() is None:
            log.warning("Killing dnsmasq immediately (PID %d)", _dnsmasq_process.pid)
            _dnsmasq_process.kill()
    else:
        log.warning("Received signal %d again, force-exiting immediately", signum)
        os._exit(1)


# Validation helpers

def construct_xname(row: int, rack: int, uslot: int) -> str:
    """Construct an xname from physical location fields."""
    slot_str = f"{rack}{uslot:02d}"
    return f"x{row}c0s{slot_str}b0n0"


def _normalize_mac(mac: str) -> str:
    """Return a MAC address normalized to uppercase with colon separators."""
    return mac.strip().upper()


def _is_valid_mac(mac: str) -> bool:
    """Return True if the MAC address matches the required colon format."""
    return bool(MAC_RE.match(_normalize_mac(mac)))


def _is_valid_ipv4(ip_str: str) -> bool:
    """Return True if the string is a valid IPv4 address."""
    try:
        ipaddress.ip_address(ip_str)
        return True
    except ValueError:
        return False


def _is_valid_hostname(hostname: str) -> bool:
    """Return True if the hostname matches the Omnia hostname regex."""
    return bool(HOSTNAME_RE.match(hostname))


def _is_non_negative_int(value: str) -> bool:
    """Return True if the value is a non-negative integer."""
    try:
        return int(value) >= 0
    except (ValueError, TypeError):
        return False


# CSV parsing and validation

def parse_and_validate_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse the admin inventory CSV and validate every row."""
    if not os.path.exists(csv_path):
        raise ValueError(f"CSV file not found: {csv_path}")

    errors = []
    entries = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ValueError("CSV file is empty or has no header row")

        fieldnames = [name.strip() for name in reader.fieldnames]
        missing = [col for col in REQUIRED_COLUMNS if col not in fieldnames]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        seen_service_tag: Dict[str, int] = {}
        seen_admin_mac: Dict[str, int] = {}
        seen_bmc_mac: Dict[str, int] = {}
        seen_admin_ip: Dict[str, int] = {}
        seen_bmc_ip: Dict[str, int] = {}
        seen_xname: Dict[str, int] = {}

        for row_num, raw_row in enumerate(reader, start=2):
            row = {k.strip(): (v.strip() if v is not None else "") for k, v in raw_row.items()}
            row_errors = []

            for col in REQUIRED_COLUMNS:
                if not row.get(col):
                    row_errors.append(f"Row {row_num}: missing required value for {col}")

            if row_errors:
                errors.extend(row_errors)
                continue

            service_tag = row["SERVICE_TAG"]
            admin_mac = _normalize_mac(row["ADMIN_MAC"])
            bmc_mac = _normalize_mac(row["BMC_MAC"])
            admin_ip = row["ADMIN_IP"]
            bmc_ip = row["BMC_IP"]
            hostname = row["HOSTNAME"]

            if not _is_valid_mac(row["ADMIN_MAC"]):
                row_errors.append(f"Row {row_num}: invalid ADMIN_MAC '{row['ADMIN_MAC']}'")
            if not _is_valid_mac(row["BMC_MAC"]):
                row_errors.append(f"Row {row_num}: invalid BMC_MAC '{row['BMC_MAC']}'")
            if not _is_valid_ipv4(admin_ip):
                row_errors.append(f"Row {row_num}: invalid ADMIN_IP '{admin_ip}'")
            if not _is_valid_ipv4(bmc_ip):
                row_errors.append(f"Row {row_num}: invalid BMC_IP '{bmc_ip}'")
            if not _is_valid_hostname(hostname):
                row_errors.append(f"Row {row_num}: invalid HOSTNAME '{hostname}'")

            for col in ("ROW", "RACK", "USLOT"):
                if not _is_non_negative_int(row[col]):
                    row_errors.append(f"Row {row_num}: {col} must be a non-negative integer ('{row[col]}')")

            if not row["FUNCTIONAL_GROUP_NAME"]:
                row_errors.append(f"Row {row_num}: FUNCTIONAL_GROUP_NAME must be non-empty")
            if not row["GROUP_NAME"]:
                row_errors.append(f"Row {row_num}: GROUP_NAME must be non-empty")

            if row_errors:
                errors.extend(row_errors)
                continue

            row = {k: v for k, v in row.items() if k in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
            for col in OPTIONAL_COLUMNS:
                row.setdefault(col, "")

            row["ADMIN_MAC"] = admin_mac
            row["BMC_MAC"] = bmc_mac
            row["ROW_INT"] = int(row["ROW"])
            row["RACK_INT"] = int(row["RACK"])
            row["USLOT_INT"] = int(row["USLOT"])
            row["XNAME"] = construct_xname(
                row["ROW_INT"], row["RACK_INT"], row["USLOT_INT"]
            )

            _check_duplicate(seen_service_tag, service_tag, row_num, "SERVICE_TAG", row_errors)
            _check_duplicate(seen_admin_mac, admin_mac, row_num, "ADMIN_MAC", row_errors)
            _check_duplicate(seen_bmc_mac, bmc_mac, row_num, "BMC_MAC", row_errors)
            _check_duplicate(seen_admin_ip, admin_ip, row_num, "ADMIN_IP", row_errors)
            _check_duplicate(seen_bmc_ip, bmc_ip, row_num, "BMC_IP", row_errors)
            _check_duplicate(seen_xname, row["XNAME"], row_num, "XNAME", row_errors)

            if row_errors:
                errors.extend(row_errors)
                continue

            entries.append(row)

    if errors:
        raise ValueError("\n".join(errors))

    if not entries:
        raise ValueError("CSV file has no data rows")

    admin_macs = {e["ADMIN_MAC"] for e in entries}
    bmc_macs = {e["BMC_MAC"] for e in entries}
    overlap_macs = admin_macs & bmc_macs
    if overlap_macs:
        raise ValueError(
            f"ADMIN_MAC and BMC_MAC sets overlap: {', '.join(sorted(overlap_macs))}"
        )

    admin_ips = {e["ADMIN_IP"] for e in entries}
    bmc_ips = {e["BMC_IP"] for e in entries}
    overlap_ips = admin_ips & bmc_ips
    if overlap_ips:
        raise ValueError(
            f"ADMIN_IP and BMC_IP sets overlap: {', '.join(sorted(overlap_ips))}"
        )

    return entries


def _check_duplicate(
    seen: Dict[str, int], value: str, row_num: int, label: str, errors: List[str]
) -> None:
    """Record a duplicate value error if the value has already been seen."""
    if value in seen:
        errors.append(
            f"Row {row_num}: duplicate {label} '{value}' (first seen at row {seen[value]})"
        )
    else:
        seen[value] = row_num


# Management interface validation

def _get_interface_info(iface: str) -> Dict[str, Optional[str]]:
    """Return IPv4 info for a local network interface."""
    exists = False
    ipv4 = None
    cidr = None

    try:
        result = subprocess.run(
            ["ip", "link", "show", iface],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            exists = True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    if exists:
        try:
            result = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", iface],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
                if match:
                    cidr = match.group(1)
                    ipv4 = cidr.split("/")[0]
                    break
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    if not exists:
        try:
            names = dict(socket.if_nameindex())
            if iface in names:
                exists = True
        except (OSError, ValueError):
            pass

    return {"exists": exists, "ipv4": ipv4, "cidr": cidr}


def validate_mgmt_interface(iface: str, entries: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Validate the management interface and warn about off-subnet BMC IPs."""
    info = _get_interface_info(iface)
    if not info["exists"]:
        raise ValueError(f"Management interface '{iface}' does not exist")
    if not info["ipv4"]:
        raise ValueError(f"Management interface '{iface}' has no IPv4 address")

    if info["cidr"]:
        network = ipaddress.ip_network(info["cidr"], strict=False)
        for entry in entries:
            bmc_ip = ipaddress.ip_address(entry["BMC_IP"])
            if bmc_ip not in network:
                log.warning(
                    "BMC_IP %s is not in the directly-connected subnet of %s (%s); "
                    "DHCP relay must be configured on the ToR switch",
                    entry["BMC_IP"],
                    iface,
                    info["cidr"],
                )

    return info


# Redfish helpers

def _redfish_get(
    url: str,
    auth: Tuple[str, str],
    timeout: int,
    retries: int = 0,
    retry_delay: int = 10,
) -> requests.Response:
    """Execute a Redfish GET request with connection retries."""
    for attempt in range(retries + 1):
        try:
            log.debug("Redfish GET %s", url)
            resp = requests.get(
                url,
                auth=auth,
                verify=False,
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            log.info("Redfish GET %s -> %d", url, resp.status_code)
            if log.isEnabledFor(logging.DEBUG):
                try:
                    log.debug("Redfish response body: %s", json.dumps(resp.json()))
                except (ValueError, TypeError):
                    log.debug("Redfish response body: %s", resp.text[:1000])
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning(
                "Redfish GET %s failed (attempt %d/%d): %s",
                url,
                attempt + 1,
                retries + 1,
                exc,
            )
            if attempt < retries:
                time.sleep(retry_delay)
            else:
                raise


def _redfish_patch(
    url: str,
    auth: Tuple[str, str],
    payload: Dict[str, Any],
    timeout: int,
    retries: int = 0,
    retry_delay: int = 10,
) -> requests.Response:
    """Execute a Redfish PATCH request with retries."""
    for attempt in range(retries + 1):
        try:
            log.debug("Redfish PATCH %s", url)
            log.debug("Redfish request body: %s", json.dumps(payload))
            resp = requests.patch(
                url,
                auth=auth,
                verify=False,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            log.info("Redfish PATCH %s -> %d", url, resp.status_code)
            if log.isEnabledFor(logging.DEBUG):
                try:
                    log.debug("Redfish response body: %s", json.dumps(resp.json()))
                except (ValueError, TypeError):
                    log.debug("Redfish response body: %s", resp.text[:1000])
            if resp.status_code == 200 or attempt == retries:
                return resp
            log.warning(
                "Redfish PATCH %s returned %d, retrying in %ds...",
                url,
                resp.status_code,
                retry_delay,
            )
            time.sleep(retry_delay)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning(
                "Redfish PATCH %s failed (attempt %d/%d): %s",
                url,
                attempt + 1,
                retries + 1,
                exc,
            )
            if attempt < retries:
                time.sleep(retry_delay)
            else:
                raise
    return resp


# Idempotency check

def check_idempotency(
    entries: List[Dict[str, Any]],
    bmc_user: str,
    bmc_pass: str,
) -> Set[str]:
    """Identify iDRACs that are already configured.

    For each entry, query the iDRAC at its configured BMC_IP. If it is
    reachable with the correct Service Tag and has a static IP matching
    BMC_IP, mark it already configured.

    Returns:
        Set of BMC_MAC addresses already configured.
    """
    already_configured: Set[str] = set()

    for entry in entries:
        bmc_ip = entry["BMC_IP"]
        attrs_url = (
            f"https://{bmc_ip}:{REDFISH_DEFAULT_PORT}"
            f"{REDFISH_MANAGER_ATTRIBUTES_ENDPOINT}"
        )
        try:
            resp = _redfish_get(
                attrs_url, (bmc_user, bmc_pass), timeout=5, retries=0, retry_delay=0
            )
        except requests.exceptions.RequestException:
            log.debug("Idempotency check: %s not reachable, will configure via DHCP", bmc_ip)
            continue

        if resp.status_code != 200:
            log.debug(
                "Idempotency check: %s returned %d, will configure via DHCP",
                bmc_ip,
                resp.status_code,
            )
            continue

        try:
            attrs = resp.json().get("Attributes", {})
        except (ValueError, TypeError):
            continue

        dhcp_enable = attrs.get("IPv4.1.DHCPEnable")
        current_ip = attrs.get("CurrentIPv4.1.Address")
        if dhcp_enable != "Disabled" or current_ip != bmc_ip:
            log.debug(
                "Idempotency check: %s not static (DHCP=%s IP=%s), will configure",
                bmc_ip,
                dhcp_enable,
                current_ip,
            )
            continue

        sys_url = f"https://{bmc_ip}:{REDFISH_DEFAULT_PORT}{REDFISH_SYSTEM_ENDPOINT}"
        try:
            resp = _redfish_get(
                sys_url, (bmc_user, bmc_pass), timeout=5, retries=0, retry_delay=0
            )
        except requests.exceptions.RequestException:
            log.debug("Idempotency check: Systems endpoint not reachable for %s", bmc_ip)
            continue

        if resp.status_code != 200:
            continue

        try:
            data = resp.json()
        except (ValueError, TypeError):
            continue

        service_tag = data.get("SKU", "")
        if service_tag and service_tag.upper() == entry["SERVICE_TAG"].upper():
            log.info(
                "SKIP: %s (%s) already configured",
                entry["HOSTNAME"],
                bmc_ip,
            )
            already_configured.add(entry["BMC_MAC"])
        else:
            log.warning(
                "Idempotency check: Service Tag mismatch for %s: expected %s, got %s",
                bmc_ip,
                entry["SERVICE_TAG"],
                service_tag,
            )

    return already_configured


# dnsmasq helpers

def check_dnsmasq_installed() -> None:
    """Verify that dnsmasq is available in PATH."""
    if not shutil.which("dnsmasq"):
        raise FileNotFoundError(
            "dnsmasq is not installed or not on PATH. Install dnsmasq to continue."
        )


def generate_dnsmasq_files(
    entries_to_serve: List[Dict[str, Any]],
    mgmt_iface: str,
    mgmt_gateway: str,
    mgmt_netmask: str,
    first_bmc_ip: str,
    log_file: str,
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create temporary dnsmasq configuration, hostsfile, and leasefile."""
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="idrac_ip_config_"))
    hosts_path = tmpdir / "dnsmasq.hosts"
    config_path = tmpdir / "dnsmasq.conf"
    lease_path = tmpdir / "dnsmasq.leases"

    with open(hosts_path, "w", encoding="utf-8") as f:
        for entry in entries_to_serve:
            f.write(f"{entry['BMC_MAC']},{entry['BMC_IP']},{entry['HOSTNAME']}\n")

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(f"interface={mgmt_iface}\n")
        f.write("bind-interfaces\n")
        f.write("no-daemon\n")
        f.write(f"dhcp-leasefile={lease_path}\n")
        f.write("log-dhcp\n")
        f.write(f"log-facility={log_file}\n")
        f.write(f"dhcp-range={first_bmc_ip},static\n")
        f.write(f"dhcp-hostsfile={hosts_path}\n")
        f.write(f"dhcp-option=3,{mgmt_gateway}\n")
        f.write(f"dhcp-option=1,{mgmt_netmask}\n")

    log.info("Generated dnsmasq config: %s", config_path)
    log.debug("dnsmasq hostsfile: %s", hosts_path)
    log.debug("dnsmasq leasefile: %s", lease_path)
    return config_path, hosts_path, lease_path, tmpdir


def start_dnsmasq(config_path: pathlib.Path) -> subprocess.Popen:
    """Start dnsmasq with the generated configuration."""
    cmd = ["dnsmasq", f"--conf-file={config_path}"]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.info("Started dnsmasq with PID %d", process.pid)

    time.sleep(1)
    ret = process.poll()
    if ret is not None:
        stdout = process.stdout.read() if process.stdout else ""
        raise RuntimeError(
            f"dnsmasq failed to start (exit code {ret}): {stdout.strip()}"
        )

    return process


def stop_dnsmasq(process: Optional[subprocess.Popen]) -> None:
    """Terminate the dnsmasq subprocess gracefully."""
    if process is None or process.poll() is not None:
        return
    log.info("Terminating dnsmasq (PID %d)", process.pid)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.warning("dnsmasq did not terminate, killing it")
        process.kill()
        process.wait()


def cleanup_temp_files(tmpdir: Optional[pathlib.Path]) -> None:
    """Delete the temporary dnsmasq directory and files."""
    if tmpdir and tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)
        log.info("Cleaned up temporary files in %s", tmpdir)


def _read_lease_macs(lease_path: pathlib.Path) -> Set[str]:
    """Read MAC addresses from the dnsmasq lease file."""
    macs: Set[str] = set()
    if not lease_path.exists():
        return macs

    with open(lease_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                mac = _normalize_mac(parts[1])
                if _is_valid_mac(mac):
                    macs.add(mac)
    return macs


# Per-iDRAC configuration

def configure_idrac(
    entry: Dict[str, Any],
    bmc_user: str,
    bmc_pass: str,
    mgmt_gateway: str,
    mgmt_netmask: str,
) -> Tuple[bool, Optional[str]]:
    """Configure a single iDRAC via Redfish."""
    bmc_ip = entry["BMC_IP"]
    hostname = entry["HOSTNAME"]
    xname = entry["XNAME"]
    auth = (bmc_user, bmc_pass)

    attrs_url = (
        f"https://{bmc_ip}:{REDFISH_DEFAULT_PORT}"
        f"{REDFISH_MANAGER_ATTRIBUTES_ENDPOINT}"
    )
    sys_url = f"https://{bmc_ip}:{REDFISH_DEFAULT_PORT}{REDFISH_SYSTEM_ENDPOINT}"
    system_attr_url = (
        f"https://{bmc_ip}:{REDFISH_DEFAULT_PORT}"
        f"{SYSTEM_EMBEDDED_ENDPOINT}"
    )

    # Wait for Redfish service to initialize after BMC reset/DHCP acquisition
    log.info("Waiting 30s for Redfish service on %s to initialize...", bmc_ip)
    time.sleep(30)

    for attempt in range(12):
        try:
            resp = _redfish_get(sys_url, auth, timeout=30, retries=0, retry_delay=0)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning(
                "Redfish verification for %s failed (attempt %d/12): %s",
                bmc_ip,
                attempt + 1,
                exc,
            )
            if attempt == 11:
                return False, f"Redfish verification failed: {exc}"
            time.sleep(10)
            continue

        if resp.status_code != 200:
            log.warning(
                "Redfish verification for %s returned %d (attempt %d/12)",
                bmc_ip,
                resp.status_code,
                attempt + 1,
            )
            if attempt == 3:
                return False, f"Redfish GET returned {resp.status_code}"
            time.sleep(10)
            continue

        try:
            data = resp.json()
        except (ValueError, TypeError) as exc:
            return False, f"Invalid JSON response: {exc}"

        service_tag = data.get("SKU", "")
        if not service_tag:
            return False, "Service Tag missing in Redfish response"
        if service_tag.upper() != entry["SERVICE_TAG"].upper():
            return False, (
                f"Service Tag mismatch: expected {entry['SERVICE_TAG']}, "
                f"got {service_tag}"
            )
        break

    # PATCH location parameters.
    location_payload = {
        "Attributes": {
            "ServerTopology.1.AisleName": str(entry["ROW_INT"]),
            "ServerTopology.1.RackName": str(entry["RACK_INT"]),
            "ServerTopology.1.RackSlot": entry["USLOT_INT"],
        }
    }
    try:
        resp = _redfish_patch(
            system_attr_url, auth, location_payload, timeout=30, retries=0, retry_delay=0
        )
        if resp.status_code != 200:
            log.warning(
                "Location PATCH for %s returned %d (continuing)",
                bmc_ip,
                resp.status_code,
            )
        else:
            log.info("Location parameters set for %s", bmc_ip)
    except requests.exceptions.RequestException as exc:
        log.warning("Location PATCH for %s failed: %s (continuing)", bmc_ip, exc)

    # PATCH static IP configuration.
    static_payload = {
        "Attributes": {
            "IPv4Static.1.Address": bmc_ip,
            "IPv4Static.1.Netmask": mgmt_netmask,
            "IPv4Static.1.Gateway": mgmt_gateway,
            "IPv4.1.DHCPEnable": "Disabled",
        }
    }
    try:
        resp = _redfish_patch(
            attrs_url,
            auth,
            static_payload,
            timeout=30,
            retries=1,
            retry_delay=10,
        )
        if resp.status_code != 200:
            return False, f"Static IP PATCH returned {resp.status_code}"
    except requests.exceptions.RequestException as exc:
        return False, f"Static IP PATCH failed: {exc}"

    # Wait and verify static IP settings.
    time.sleep(15)
    gateway_retry_done = False
    for attempt in range(4):
        try:
            resp = _redfish_get(
                attrs_url, auth, timeout=30, retries=0, retry_delay=0
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            log.warning(
                "Verification GET for %s failed (attempt %d/4): %s",
                bmc_ip,
                attempt + 1,
                exc,
            )
            if attempt == 3:
                return False, f"Static IP verification failed: {exc}"
            time.sleep(10)
            continue

        if resp.status_code != 200:
            log.warning(
                "Verification GET for %s returned %d (attempt %d/4)",
                bmc_ip,
                resp.status_code,
                attempt + 1,
            )
            if attempt == 3:
                return False, f"Verification GET returned {resp.status_code}"
            time.sleep(10)
            continue

        try:
            attrs = resp.json().get("Attributes", {})
        except (ValueError, TypeError) as exc:
            if attempt == 3:
                return False, f"Invalid JSON in verification response: {exc}"
            time.sleep(10)
            continue

        current_ip = attrs.get("CurrentIPv4.1.Address")
        dhcp_enable = attrs.get("IPv4.1.DHCPEnable")
        current_gw = attrs.get("CurrentIPv4.1.Gateway")

        if current_ip == bmc_ip and dhcp_enable == "Disabled":
            if current_gw == mgmt_gateway:
                log.info("Static IP verified for %s: %s", bmc_ip, current_ip)
                break
            if not gateway_retry_done and attempt < 3:
                log.warning(
                    "Gateway mismatch for %s: expected %s, got %s; re-PATCHing",
                    bmc_ip,
                    mgmt_gateway,
                    current_gw,
                )
                try:
                    resp = _redfish_patch(
                        attrs_url,
                        auth,
                        static_payload,
                        timeout=30,
                        retries=0,
                        retry_delay=0,
                    )
                except requests.exceptions.RequestException as exc:
                    return False, f"Gateway retry PATCH failed: {exc}"
                if resp.status_code != 200:
                    return False, f"Gateway retry PATCH returned {resp.status_code}"
                gateway_retry_done = True
                time.sleep(10)
                continue
            return False, f"Gateway mismatch: {current_gw}"

        log.warning(
            "Static IP not yet applied for %s (attempt %d/4): "
            "IP=%s DHCP=%s",
            bmc_ip,
            attempt + 1,
            current_ip,
            dhcp_enable,
        )
        if attempt == 3:
            return False, "Static IP verification failed after 3 retries"
        time.sleep(10)

    log.info("iDRAC configured for %s (%s) -> %s", hostname, bmc_ip, xname)
    return True, None


# Discovery and configuration loop

def poll_and_configure(
    entries_to_serve: List[Dict[str, Any]],
    lease_path: pathlib.Path,
    bmc_user: str,
    bmc_pass: str,
    mgmt_gateway: str,
    mgmt_netmask: str,
    workers: int,
    timeout: int,
) -> Tuple[Set[str], Dict[str, str]]:
    """Poll dnsmasq leases and configure iDRACs in parallel."""
    mac_to_entry = {e["BMC_MAC"]: e for e in entries_to_serve}
    expected_macs = set(mac_to_entry.keys())
    served_macs: Set[str] = set()
    failed_macs: Dict[str, str] = {}
    futures: Dict[Any, str] = {}

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            if shutdown_requested.is_set():
                log.info("Shutdown requested, exiting discovery loop")
                break

            elapsed = time.time() - start_time
            if elapsed > timeout:
                not_served = expected_macs - served_macs - set(failed_macs.keys())
                log.warning(
                    "Timeout reached after %d seconds. Not served MACs: %s",
                    int(elapsed),
                    sorted(not_served),
                )
                break

            if (len(served_macs) + len(failed_macs)) >= len(expected_macs):
                break

            # Process completed futures.
            done_futures = [f for f in list(futures.keys()) if f.done()]
            for future in done_futures:
                mac = futures.pop(future)
                try:
                    success, reason = future.result()
                    entry = mac_to_entry[mac]
                    if success:
                        served_macs.add(mac)
                        pct = int(100 * len(served_macs) / len(expected_macs)) if expected_macs else 100
                        log.info(
                            "PROGRESS: %d/%d iDRACs configured (%d%%) - %s (%s) -> %s",
                            len(served_macs),
                            len(expected_macs),
                            pct,
                            entry["HOSTNAME"],
                            entry["BMC_IP"],
                            entry["XNAME"],
                        )
                    else:
                        failed_macs[mac] = reason
                        log.error("Failed to configure %s: %s", mac, reason)
                except Exception as exc:  # pylint: disable=broad-except
                    failed_macs[mac] = str(exc)
                    log.exception("Unexpected error configuring %s", mac)

            if (len(served_macs) + len(failed_macs)) >= len(expected_macs):
                break

            # Submit tasks for any newly seen leases.
            in_flight = set(futures.values())
            lease_macs = _read_lease_macs(lease_path)
            new_macs = lease_macs - served_macs - set(failed_macs.keys()) - in_flight
            for mac in new_macs:
                if mac in mac_to_entry:
                    entry = mac_to_entry[mac]
                    log.info(
                        "Detected DHCP lease for %s (%s), starting configuration",
                        mac,
                        entry["HOSTNAME"],
                    )
                    future = executor.submit(
                        configure_idrac,
                        entry,
                        bmc_user,
                        bmc_pass,
                        mgmt_gateway,
                        mgmt_netmask,
                    )
                    futures[future] = mac
                else:
                    log.warning("DHCP lease for unknown MAC %s, ignoring", mac)

            time.sleep(2)

        # Wait for in-flight futures to complete.
        if futures:
            if shutdown_requested.is_set():
                log.warning("Shutdown requested, cancelling %d in-flight configurations", len(futures))
                for future in futures:
                    future.cancel()
            else:
                log.info("Waiting for %d in-flight configurations to complete...", len(futures))

            remaining = [f for f in futures if not f.cancelled()]
            try:
                for future in as_completed(remaining, timeout=30):
                    if shutdown_requested.is_set():
                        log.warning("Shutdown requested during final wait, stopping early")
                        break
                    mac = futures[future]
                    try:
                        success, reason = future.result(timeout=5)
                        if success:
                            served_macs.add(mac)
                        else:
                            failed_macs[mac] = reason
                    except TimeoutError:
                        failed_macs[mac] = "Result timeout after 5s"
                        log.warning("Timeout waiting for result from %s", mac)
                    except Exception as exc:  # pylint: disable=broad-except
                        failed_macs[mac] = str(exc)
                        log.exception("Unexpected error retrieving result for %s", mac)
            except TimeoutError:
                log.warning("Timeout waiting for in-flight configurations after 30s")
                for future, mac in futures.items():
                    if not future.done():
                        failed_macs[mac] = "Configuration timeout"

    return served_macs, failed_macs


# Summary

def print_summary(
    total: int,
    already_configured: Set[str],
    served_macs: Set[str],
    failed_macs: Dict[str, str],
    entries: List[Dict[str, Any]],
    log_file: str,
) -> int:
    """Print a summary to stdout and return the appropriate exit code."""
    newly = len(served_macs)
    already = len(already_configured)
    failed = len(failed_macs)
    all_done = already_configured | served_macs
    all_failed = set(failed_macs.keys())
    not_served_macs = {e["BMC_MAC"] for e in entries} - all_done - all_failed
    not_served = len(not_served_macs)

    entries_by_mac = {e["BMC_MAC"]: e for e in entries}

    print("iDRAC IP Configuration Complete")
    print(f"Total expected: {total}")
    print(f"Already configured: {already}")
    print(f"Newly configured: {newly}")
    print(f"Failed: {failed}")
    print(f"Not served (timeout): {not_served}")
    print(f"Log file: {log_file}")

    if failed > 0:
        print("Failed iDRACs:")
        for mac, reason in sorted(failed_macs.items()):
            entry = entries_by_mac.get(mac, {})
            print(
                f"  {mac}, {entry.get('BMC_IP', '')}, {entry.get('SERVICE_TAG', '')}, "
                f"{reason}"
            )

    if not_served > 0:
        print("Not served (check cabling/power):")
        for mac in sorted(not_served_macs):
            entry = entries_by_mac.get(mac, {})
            print(f"  {mac}, {entry.get('BMC_IP', '')}, {entry.get('SERVICE_TAG', '')}")

    return 0 if (failed == 0 and not_served == 0) else 1


# CLI and main

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Automate iDRAC BMC network setup for Omnia provisioning."
    )
    parser.add_argument(
        "--inventory",
        required=True,
        help="Path to the admin inventory CSV file.",
    )
    parser.add_argument(
        "--mgmt-iface",
        required=True,
        help="Network interface connected to the iDRAC management network.",
    )
    parser.add_argument(
        "--mgmt-gateway",
        required=True,
        help="Gateway IP for the iDRAC management subnet.",
    )
    parser.add_argument(
        "--mgmt-netmask",
        required=True,
        help="Netmask for the iDRAC management subnet (e.g., 255.255.254.0).",
    )
    parser.add_argument(
        "--bmc-user",
        default="root",
        help="Redfish username for iDRAC authentication (default: root).",
    )
    parser.add_argument(
        "--bmc-pass",
        default="calvin",
        help="Redfish password for iDRAC authentication (default: calvin).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Maximum seconds to wait for all iDRACs (default: 1800).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent Redfish threads (default: 10).",
    )
    parser.add_argument(
        "--log-file",
        default="idrac_ip_config.log",
        help="Path to the log file (default: idrac_ip_config.log).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print summary without making changes.",
    )

    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be a positive integer")
    if args.workers <= 0:
        parser.error("--workers must be a positive integer")

    return args


def _setup_logging(log_file: str, log_level: str) -> None:
    """Configure logging to stderr and a file."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handlers = [logging.StreamHandler(), logging.FileHandler(log_file, mode="a")]
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    log.setLevel(level)


def main() -> int:
    """Main entry point for the iDRAC IP configuration script."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    args = _parse_args()
    _setup_logging(args.log_file, args.log_level)

    log.info("=" * 60)
    log.info("Starting iDRAC IP configuration")
    log.info("=" * 60)

    # Phase 1: Validate CSV.
    try:
        entries = parse_and_validate_csv(args.inventory)
    except ValueError as exc:
        log.error("CSV validation failed: %s", exc)
        return 1

    total_csv = len(entries)
    log.info("Validated %d entries from %s", total_csv, args.inventory)

    # Validate management interface.
    try:
        validate_mgmt_interface(args.mgmt_iface, entries)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    # Verify dnsmasq is installed.
    try:
        check_dnsmasq_installed()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    # Dry run: validate only and exit before any network calls.
    if args.dry_run:
        print("DRY RUN: No changes made")
        print(f"Total iDRACs in CSV: {total_csv}")
        print("Already configured: not checked (dry-run)")
        print("To configure via DHCP: not determined (dry-run)")
        return 0

    # Idempotency check.
    log.info("Checking for already configured iDRACs...")
    already_configured = check_idempotency(
        entries,
        args.bmc_user,
        args.bmc_pass,
    )
    log.info("Already configured iDRACs: %d", len(already_configured))

    # Phase 2: Prepare DHCP server.
    entries_to_serve = [e for e in entries if e["BMC_MAC"] not in already_configured]

    if not entries_to_serve:
        log.info("All iDRACs already configured; skipping DHCP and Redfish.")
        return print_summary(
            total_csv,
            already_configured,
            set(),
            {},
            entries,
            args.log_file,
        )

    first_bmc_ip = entries_to_serve[0]["BMC_IP"]
    config_path, hosts_path, lease_path, tmpdir = generate_dnsmasq_files(
        entries_to_serve,
        args.mgmt_iface,
        args.mgmt_gateway,
        args.mgmt_netmask,
        first_bmc_ip,
        args.log_file,
    )

    dnsmasq_process: Optional[subprocess.Popen] = None
    served_macs: Set[str] = set()
    failed_macs: Dict[str, str] = {}

    try:
        # Start DHCP server.
        dnsmasq_process = start_dnsmasq(config_path)
        global _dnsmasq_process
        _dnsmasq_process = dnsmasq_process
        log.info(
            "Started DHCP server for %d iDRAC(s) awaiting service",
            len(entries_to_serve),
        )

        # Discovery and configuration loop.
        served_macs, failed_macs = poll_and_configure(
            entries_to_serve,
            lease_path,
            args.bmc_user,
            args.bmc_pass,
            args.mgmt_gateway,
            args.mgmt_netmask,
            args.workers,
            args.timeout,
        )
    except Exception as exc:  # pylint: disable=broad-except
        log.exception("Unexpected error during DHCP/Redfish phase: %s", exc)
    finally:
        # Cleanup.
        stop_dnsmasq(dnsmasq_process)
        cleanup_temp_files(tmpdir)

    return print_summary(
        total_csv,
        already_configured,
        served_macs,
        failed_macs,
        entries,
        args.log_file,
    )


if __name__ == "__main__":
    sys.exit(main())
