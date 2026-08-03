#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iDRAC lease watcher for Omnia/OpenCHAMI.

Polls the CoreDHCP bootloop lease database for new DHCP leases, discovers
iDRACs via Redfish, and burns the intended static BMC IP plus physical
location metadata.
"""

import argparse
import csv
import ipaddress
import json
import logging
import os
import pathlib
import signal
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from inventory_expander import load_sparse_inventory, save_complete_inventory_csv

try:
    import requests
except ImportError:  # pragma: no cover - guard for missing runtime dependency
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


REDFISH_SYSTEM_ENDPOINT = "/redfish/v1/Systems/System.Embedded.1"
REDFISH_MANAGER_ATTRIBUTES_ENDPOINT = "/redfish/v1/Managers/iDRAC.Embedded.1/Attributes"
REDFISH_DEFAULT_PORT = 443
SYSTEM_EMBEDDED_ENDPOINT = "/redfish/v1/Managers/iDRAC.Embedded.1/Oem/Dell/DellAttributes/System.Embedded.1"

log = logging.getLogger("idrac_watcher")
shutdown_requested = threading.Event()


def _signal_handler(signum: int, _frame: Any) -> None:
    """Handle SIGINT/SIGTERM by requesting graceful shutdown."""
    log.info("Received signal %d, requesting graceful shutdown", signum)
    shutdown_requested.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


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


def _redfish_get(
    url: str,
    auth: Tuple[str, str],
    timeout: int,
    retries: int = 0,
    retry_delay: int = 10,
) -> requests.Response:
    """Execute a Redfish GET request with connection retries."""
    last_exc = None
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
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            log.warning(
                "Redfish GET %s failed (attempt %d/%d): %s",
                url,
                attempt + 1,
                retries + 1,
                exc,
            )
            if attempt < retries:
                time.sleep(retry_delay)
    raise last_exc  # type: ignore[misc]


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
            resp = requests.patch(
                url,
                auth=auth,
                verify=False,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            log.info("Redfish PATCH %s -> %d", url, resp.status_code)
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


def get_service_tag(temp_ip: str, bmc_user: str, bmc_pass: str) -> Optional[str]:
    """Return the iDRAC service tag (SKU) for a temp BMC IP, or None."""
    url = f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{REDFISH_SYSTEM_ENDPOINT}"
    try:
        resp = _redfish_get(url, (bmc_user, bmc_pass), timeout=15, retries=0)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, TypeError):
        return None

    service_tag = data.get("SKU", "")
    return service_tag.strip() if service_tag else None


def _lookup_subnet(bmc_ip: str, subnets: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (gateway, netmask) for the subnet that contains bmc_ip."""
    ip = ipaddress.ip_address(bmc_ip)
    for s in subnets:
        cidr = s.get("cidr")
        if not cidr:
            subnet = s.get("subnet")
            netmask_bits = s.get("netmask_bits")
            if subnet is None or netmask_bits is None:
                continue
            cidr = f"{subnet}/{netmask_bits}"
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid subnet CIDR {cidr}: {exc}") from exc
        if ip in network:
            gateway = s.get("router", s.get("gateway", ""))
            netmask = str(network.netmask)
            return gateway, netmask
    raise ValueError(f"BMC IP {bmc_ip} does not belong to any configured subnet")


def configure_idrac(
    entry: Dict[str, Any],
    temp_ip: str,
    bmc_user: str,
    bmc_pass: str,
    subnets: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Burn the static IP and location metadata for one iDRAC."""
    auth = (bmc_user, bmc_pass)
    service_tag = entry["SERVICE_TAG"]

    # Verify service tag before making changes
    seen_tag = get_service_tag(temp_ip, bmc_user, bmc_pass)
    if not seen_tag:
        return False, "Unable to retrieve service tag from Redfish"
    if seen_tag.upper() != service_tag.upper():
        return False, f"Service tag mismatch: expected {service_tag}, got {seen_tag}"

    gateway, netmask = _lookup_subnet(entry["BMC_IP"], subnets)

    # PATCH location parameters
    location_payload = {
        "Attributes": {
            "ServerTopology.1.AisleName": str(entry["ROW_INT"]),
            "ServerTopology.1.RackName": str(entry["RACK_INT"]),
            "ServerTopology.1.RackSlot": entry["USLOT_INT"],
        }
    }
    system_attr_url = (
        f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{SYSTEM_EMBEDDED_ENDPOINT}"
    )
    try:
        resp = _redfish_patch(system_attr_url, auth, location_payload, timeout=30, retries=0)
        if resp.status_code != 200:
            log.warning("Location PATCH for %s returned %d", temp_ip, resp.status_code)
        else:
            log.info("Location parameters set for %s", temp_ip)
    except requests.exceptions.RequestException as exc:
        log.warning("Location PATCH for %s failed: %s", temp_ip, exc)

    # PATCH static IP configuration
    attrs_url = (
        f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{REDFISH_MANAGER_ATTRIBUTES_ENDPOINT}"
    )
    static_payload = {
        "Attributes": {
            "IPv4Static.1.Address": entry["BMC_IP"],
            "IPv4Static.1.Netmask": netmask,
            "IPv4Static.1.Gateway": gateway,
            "IPv4.1.DHCPEnable": "Disabled",
        }
    }
    try:
        resp = _redfish_patch(attrs_url, auth, static_payload, timeout=30, retries=1, retry_delay=10)
        if resp.status_code != 200:
            return False, f"Static IP PATCH returned {resp.status_code}"
    except requests.exceptions.RequestException as exc:
        return False, f"Static IP PATCH failed: {exc}"

    # Wait for the patched static IP attributes to be reflected before declaring success.
    time.sleep(15)
    verify_ip = entry["BMC_IP"]
    verified = False
    current_ip = ""
    current_gw = ""
    dhcp_enable = ""
    for attempt in range(4):
        for candidate in (verify_ip, temp_ip):
            if not candidate:
                continue
            verify_url = (
                f"https://{candidate}:{REDFISH_DEFAULT_PORT}"
                f"{REDFISH_MANAGER_ATTRIBUTES_ENDPOINT}"
            )
            try:
                verify_resp = _redfish_get(verify_url, auth, timeout=30, retries=0)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                continue
            if verify_resp.status_code != 200:
                continue
            try:
                attrs = verify_resp.json().get("Attributes", {})
            except (ValueError, TypeError):
                continue

            dhcp_enable = attrs.get("IPv4.1.DHCPEnable", "")
            current_ip = (
                attrs.get("IPv4Static.1.Address", "")
                or attrs.get("CurrentIPv4.1.Address", "")
            )
            current_gw = (
                attrs.get("IPv4Static.1.Gateway", "")
                or attrs.get("CurrentIPv4.1.Gateway", "")
            )

            if (
                dhcp_enable == "Disabled"
                and current_ip == verify_ip
                and current_gw == gateway
            ):
                verified = True
                break
        if verified:
            break
        log.warning(
            "Static IP not reflected for %s (attempt %d/4): DHCP=%s IP=%s GW=%s",
            verify_ip,
            attempt + 1,
            dhcp_enable,
            current_ip,
            current_gw,
        )
        if attempt < 3:
            time.sleep(10)

    if not verified:
        return False, f"Static IP not reflected for {verify_ip}"

    log.info(
        "Static IP burned and verified for %s: %s/%s via gateway %s",
        verify_ip,
        netmask,
        gateway,
    )
    return True, ""


def read_coredhcp_leases(db_path: str) -> List[Tuple[str, str]]:
    """Return (mac, ip) tuples for current IPv4 leases from the bootloop DB."""
    if not os.path.exists(db_path):
        return []

    leases: List[Tuple[str, str]] = []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        cur = conn.cursor()
        now = int(time.time())
        cur.execute(
            "SELECT mac, ip FROM leases4 WHERE expiry > ? ORDER BY expiry DESC",
            (now,),
        )
        leases = [(row[0], row[1]) for row in cur.fetchall()]
        conn.close()
    except sqlite3.Error as exc:
        log.warning("Failed to read coredhcp lease DB %s: %s", db_path, exc)
    return leases


def process_lease(
    mac: str,
    ip: str,
    dense: List[Dict[str, Any]],
    by_service_tag: Dict[str, Dict[str, Any]],
    bmc_user: str,
    bmc_pass: str,
    subnets: List[Dict[str, Any]],
) -> Tuple[str, bool, str]:
    """Probe and configure a single lease. Returns (mac, success, message)."""
    log.info("New lease %s -> %s", mac, ip)
    service_tag = get_service_tag(ip, bmc_user, bmc_pass)
    if not service_tag:
        return mac, False, f"{ip}: not an iDRAC or Redfish unreachable"

    entry = by_service_tag.get(service_tag.upper())
    if not entry:
        return mac, False, f"{ip}: service tag {service_tag} not in inventory"

    success, msg = configure_idrac(entry, ip, bmc_user, bmc_pass, subnets)
    return mac, success, msg


def main_loop(
    db_path: str,
    dense: List[Dict[str, Any]],
    bmc_user: str,
    bmc_pass: str,
    subnets: List[Dict[str, Any]],
    concurrency: int,
    poll_interval: int,
) -> None:
    """Poll lease DB until every iDRAC in the inventory is served."""
    by_service_tag = {e["SERVICE_TAG"].upper(): e for e in dense}
    served_macs: set = set()
    in_progress: set = set()
    failed_macs: Dict[str, float] = {}
    total = len(dense)
    retry_cooldown = 60.0

    log.info("Waiting for %d iDRAC(s)", total)

    while not shutdown_requested.is_set():
        if len(served_macs) >= total:
            log.info("All %d iDRAC(s) served", total)
            break

        now = time.time()
        leases = read_coredhcp_leases(db_path)
        pending: List[Tuple[str, str]] = []
        for mac, ip in leases:
            if mac in served_macs or mac in in_progress:
                continue
            # Retry failed MACs only after a cooldown to avoid hammering
            if mac in failed_macs and (now - failed_macs[mac]) < retry_cooldown:
                continue
            pending.append((mac, ip))

        if not pending:
            time.sleep(poll_interval)
            continue

        for mac, _ in pending:
            in_progress.add(mac)
            failed_macs[mac] = now

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    process_lease, mac, ip, dense, by_service_tag, bmc_user, bmc_pass, subnets
                ): (mac, ip)
                for mac, ip in pending
            }
            for future in as_completed(futures):
                mac, ip = futures[future]
                in_progress.discard(mac)
                try:
                    _mac, success, msg = future.result(timeout=300)
                    if success:
                        served_macs.add(mac)
                        failed_macs.pop(mac, None)
                        log.info("Served %s (%s)", mac, ip)
                    else:
                        log.warning("Failed to serve %s (%s): %s", mac, ip, msg)
                except Exception as exc:  # pragma: no cover - catch worker exceptions
                    log.error("Worker exception for %s (%s): %s", mac, ip, exc)

        log.info("Progress: %d/%d iDRAC(s) served", len(served_macs), total)
        time.sleep(poll_interval)


def load_config(path: str) -> Dict[str, Any]:
    """Load runtime JSON configuration."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="iDRAC DHCP lease watcher")
    parser.add_argument("--config", required=True, help="Path to JSON configuration")
    args = parser.parse_args()

    config = load_config(args.config)

    log_level = config.get("log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s: %(message)s",
        stream=sys.stdout,
    )
    log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    csv_path = config["inventory_csv"]
    complete_path = config["complete_inventory_csv"]
    db_path = config["lease_db"]
    bmc_user = config.get("bmc_username", "root")
    bmc_pass = config.get("bmc_password", "calvin")
    subnets = config.get("subnets", [])
    concurrency = int(config.get("concurrency", 5))
    poll_interval = int(config.get("poll_interval", 2))

    dense = load_sparse_inventory(csv_path)
    save_complete_inventory_csv(complete_path, dense)
    log.info("Complete inventory written to %s (%d entries)", complete_path, len(dense))

    main_loop(db_path, dense, bmc_user, bmc_pass, subnets, concurrency, poll_interval)

    if shutdown_requested.is_set():
        log.info("Shutdown requested, exiting")
        return 0

    log.info("iDRAC watcher completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
