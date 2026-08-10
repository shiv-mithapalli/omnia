#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vendor-agnostic BMC lease handler for CoreDHCP/bootloop.

Invoked once per new DHCP lease by the bootloop on_lease_script hook.
Uses Redfish to identify the BMC, verify its service tag against an
inventory, and burn the intended static IP and location metadata.

Usage:
    bmc_lease_handler.py --mac <mac> --ip <temp_ip> --giaddr <giaddr> [--cidr <cidr>]
"""

# SPDX-FileCopyrightText: © 2026 OpenCHAMI a Series of LF Projects, LLC
#
# SPDX-License-Identifier: MIT

import argparse
import csv
import ipaddress
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover - guard for missing runtime dependency
    print("ERROR: 'requests' package is required.")
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

DEFAULT_CONFIG = "/config/bmc_config.json"
REDFISH_DEFAULT_PORT = 443

# Exit codes consumed by the CoreDHCP bootloop on_lease_script hook.
# 0: this lease is not for a BMC we need to configure; keep the lease as normal.
# 1: provisioning failed before the static IP PATCH was accepted; keep the
#    temporary lease so the BMC can renew, get NAKed, and retry.
# 2: the static IP PATCH was accepted; the BMC should no longer use DHCP, so
#    release the temporary lease. A best-effort verification may run first.
EXIT_SKIP = 0
EXIT_FAIL = 1
EXIT_RELEASE = 2

log = logging.getLogger("bmc_lease_handler")


def _is_valid_ipv4(ip_str: str) -> bool:
    """Return True if the string is a valid IPv4 address."""
    try:
        ipaddress.IPv4Address(ip_str)
        return True
    except (ValueError, TypeError):
        return False


def redfish_get(
    url: str,
    auth: Tuple[str, str],
    timeout: int,
    retries: int = 0,
    retry_delay: int = 10,
) -> requests.Response:
    """Execute a Redfish GET request with retries on server errors.

    Retries only on 5xx responses and network-level errors (connection/timeout).
    4xx client errors are returned immediately because they are not expected to
    succeed on retry (e.g. 404 means the endpoint does not exist, 401/403 is an
    auth failure).
    """
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
            if 200 <= resp.status_code < 300:
                return resp
            if resp.status_code >= 500:
                if attempt < retries:
                    log.warning(
                        "Redfish GET %s returned %d, retrying in %ds...",
                        url,
                        resp.status_code,
                        retry_delay,
                    )
                    time.sleep(retry_delay)
                    continue
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


def redfish_patch(
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
            if 200 <= resp.status_code < 300 or attempt == retries:
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


def load_config(path: str) -> Dict[str, Any]:
    """Load runtime JSON configuration."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_inventory(csv_path: str) -> List[Dict[str, Any]]:
    """Load a pre-expanded BMC inventory CSV."""
    rows: List[Dict[str, Any]] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({k.strip(): (v.strip() if v is not None else "") for k, v in row.items()})
    return rows


def get_service_tag(
    temp_ip: str,
    bmc_user: str,
    bmc_pass: str,
    profile: Dict[str, Any],
    timeout: int = 30,
    retries: int = 0,
) -> Optional[str]:
    """Return the service tag (or identifier) for a temp BMC IP, or None."""
    system_endpoint = profile.get("system_endpoint", "/redfish/v1/Systems/1")
    url = f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{system_endpoint}"
    try:
        resp = redfish_get(url, (bmc_user, bmc_pass), timeout=timeout, retries=retries)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        return None

    if resp.status_code in (401, 403):
        raise requests.exceptions.HTTPError(
            f"Redfish authentication failed for {temp_ip}: {resp.status_code}"
        )

    if resp.status_code >= 500:
        raise requests.exceptions.HTTPError(
            f"Redfish probe for {temp_ip} returned server error {resp.status_code}"
        )

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    field = profile.get("service_tag_field", "SKU")
    service_tag = data.get(field, "") or ""
    return service_tag.strip() if service_tag else None


def lookup_subnet(bmc_ip: str, subnets: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (gateway, netmask) for the subnet that contains bmc_ip."""
    ip = ipaddress.ip_address(bmc_ip)
    for s in subnets:
        subnet = s.get("subnet", "").split("/")[0].strip()
        netmask_bits = s.get("netmask_bits")
        if not subnet or not netmask_bits:
            continue
        try:
            network = ipaddress.ip_network(f"{subnet}/{netmask_bits}", strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid subnet CIDR {subnet}/{netmask_bits}: {exc}") from exc
        if ip in network:
            gateway = (s.get("router") or s.get("gateway") or "")
            netmask = str(network.netmask)
            return gateway, netmask
    raise ValueError(f"BMC IP {bmc_ip} does not belong to any configured subnet")


def build_bmc_hostname(
    entry: Dict[str, Any],
    name_format: str = "{GROUP_NAME}R{RACK}OU{USLOT}C1",
) -> Optional[str]:
    """Build a BMC/iDRAC hostname from inventory location fields.

    The default format follows the Omnia SU hostname convention, e.g.
    ``SU1R2OU1C5``. GROUP_NAME is expected to be the SU name; RACK and USLOT
    come from the admin inventory location columns.

    Returns None if any required field is missing or the format is invalid.
    """
    values = {
        k: str(entry.get(k, "") or "").strip()
        for k in ("GROUP_NAME", "RACK", "USLOT")
    }
    if not all(values.values()):
        return None
    try:
        return name_format.format(**values)
    except (AttributeError, KeyError, ValueError) as exc:
        log.warning(
            "Failed to build BMC hostname from %s with format %r: %s",
            values,
            name_format,
            exc,
        )
        return None


def get_vendor_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the selected vendor profile, merged with a generic fallback."""
    profiles = config.get("vendor_profiles", {})
    name = config.get("vendor_profile", "generic")

    # Built-in generic profile provides safe defaults.
    generic = {
        "system_endpoint": "/redfish/v1/Systems/1",
        "manager_attributes_endpoint": "/redfish/v1/Managers/1/Attributes",
        "location_endpoint": None,
        "service_tag_field": "SKU",
        "static_ip_keys": {
            "address": "IPv4Static.1.Address",
            "netmask": "IPv4Static.1.Netmask",
            "gateway": "IPv4Static.1.Gateway",
            "dhcp_enable": "IPv4.1.DHCPEnable",
        },
        "dhcp_enable_flag": "Disabled",
        "location_fields": {},
        "idrac_name_key": "iDRAC.NIC.DNSRacName",
        "idrac_name_format": "{GROUP_NAME}R{RACK}OU{USLOT}C1",
    }

    selected = profiles.get(name, {})
    merged = generic.copy()
    for key, value in selected.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def configure_bmc(
    entry: Dict[str, Any],
    temp_ip: str,
    bmc_user: str,
    bmc_pass: str,
    subnets: List[Dict[str, Any]],
    profile: Dict[str, Any],
    timeout: int = 30,
    retries: int = 0,
    expected_tag: Optional[str] = None,
) -> Tuple[int, str]:
    """Burn the static IP and location metadata for one BMC.

    When expected_tag is provided, the Redfish service-tag probe is skipped;
    the caller is responsible for having already verified the BMC identity.

    Returns (exit_code, message) where exit_code is one of EXIT_SKIP, EXIT_FAIL,
    or EXIT_RELEASE (see module-level constants).
    """
    auth = (bmc_user, bmc_pass)
    service_tag = entry["SERVICE_TAG"]

    if expected_tag is None:
        try:
            seen_tag = get_service_tag(temp_ip, bmc_user, bmc_pass, profile, timeout=timeout, retries=retries)
        except requests.exceptions.RequestException as exc:
            return EXIT_FAIL, f"Redfish probe failed for {temp_ip}: {exc}"
    else:
        seen_tag = expected_tag

    if not seen_tag:
        return EXIT_FAIL, "Unable to retrieve service tag from Redfish"
    if seen_tag.upper() != service_tag.upper():
        return EXIT_FAIL, f"Service tag mismatch: expected {service_tag}, got {seen_tag}"

    try:
        gateway, netmask = lookup_subnet(entry["BMC_IP"], subnets)
    except ValueError as exc:
        return EXIT_FAIL, str(exc)

    manager_url = f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{profile['manager_attributes_endpoint']}"

    # Optional location metadata patch
    location_fields = profile.get("location_fields", {})
    location_endpoint = profile.get("location_endpoint")
    inventory_has_location = any(
        str(entry.get(k, "")).strip() for k in ("ROW", "RACK", "USLOT")
    )
    profile_has_location = bool(location_endpoint) and bool(location_fields)

    if inventory_has_location and profile_has_location:
        attrs = {}
        if "aisle" in location_fields:
            row_val = str(entry.get("ROW", "")).strip()
            if row_val:
                attrs[location_fields["aisle"]] = row_val
        if "rack" in location_fields:
            rack_val = str(entry.get("RACK", "")).strip()
            if rack_val:
                attrs[location_fields["rack"]] = rack_val
        if "slot" in location_fields:
            uslot_val = str(entry.get("USLOT", "")).strip()
            if uslot_val:
                attrs[location_fields["slot"]] = int(uslot_val)

        if attrs:
            location_url = f"https://{temp_ip}:{REDFISH_DEFAULT_PORT}{location_endpoint}"
            try:
                resp = redfish_patch(location_url, auth, {"Attributes": attrs}, timeout=timeout, retries=retries)
                if 200 <= resp.status_code < 300:
                    log.info("Location parameters set for %s", temp_ip)
                else:
                    log.warning("Location PATCH for %s returned %d", temp_ip, resp.status_code)
            except requests.exceptions.RequestException as exc:
                log.warning("Location PATCH for %s failed: %s", temp_ip, exc)
    elif inventory_has_location and not profile_has_location:
        log.warning(
            "Location data present in inventory for %s but location_endpoint/location_fields "
            "not configured in bmc_redfish_config.csv; skipping location patch",
            service_tag,
        )

    # Static IP configuration
    static_keys = profile.get("static_ip_keys", {})
    static_payload = {
        "Attributes": {
            static_keys.get("address", "IPv4Static.1.Address"): entry["BMC_IP"],
            static_keys.get("netmask", "IPv4Static.1.Netmask"): netmask,
            static_keys.get("gateway", "IPv4Static.1.Gateway"): gateway,
            static_keys.get("dhcp_enable", "IPv4.1.DHCPEnable"): profile.get("dhcp_enable_flag", "Disabled"),
        }
    }

    # Optional BMC/iDRAC hostname. GROUP_NAME is treated as the SU name and
    # combined with RACK/USLOT to match the Omnia hostname convention.
    idrac_name_key = profile.get("idrac_name_key")
    if idrac_name_key:
        name_format = profile.get("idrac_name_format") or "{GROUP_NAME}R{RACK}OU{USLOT}C1"
        bmc_name = build_bmc_hostname(entry, name_format)
        if bmc_name:
            static_payload["Attributes"][idrac_name_key] = bmc_name
            log.info("Built BMC hostname for %s: %s", service_tag, bmc_name)
        else:
            log.warning(
                "Unable to build BMC hostname for %s; ensure GROUP_NAME, RACK, and USLOT are set",
                service_tag,
            )

    static_ip_patched = False
    verify_ip = entry["BMC_IP"]

    try:
        resp = redfish_patch(manager_url, auth, static_payload, timeout=timeout, retries=retries)
        if not (200 <= resp.status_code < 300):
            return EXIT_FAIL, f"Static IP PATCH returned {resp.status_code}"
        static_ip_patched = True

        # Give the BMC time to apply the static IP and re-initialize its network
        # stack before releasing the temporary lease. Then perform a single
        # best-effort verification on the static IP. The PATCH was already
        # accepted, so the lease is released regardless of verification result.
        time.sleep(20)

        verified = False
        current_ip = ""
        current_gw = ""
        dhcp_enable = ""

        verify_url = f"https://{verify_ip}:{REDFISH_DEFAULT_PORT}{profile['manager_attributes_endpoint']}"
        try:
            verify_resp = redfish_get(verify_url, auth, timeout=timeout, retries=retries)
            if verify_resp.status_code == 200:
                try:
                    verify_data = verify_resp.json()
                except (ValueError, TypeError):
                    verify_data = None
                if isinstance(verify_data, dict):
                    attrs = verify_data.get("Attributes", {})
                    dhcp_enable = attrs.get(static_keys.get("dhcp_enable", "IPv4.1.DHCPEnable"), "")
                    current_ip = attrs.get(
                        static_keys.get("address", "IPv4Static.1.Address"), ""
                    ) or attrs.get("CurrentIPv4.1.Address", "")
                    current_gw = attrs.get(
                        static_keys.get("gateway", "IPv4Static.1.Gateway"), ""
                    ) or attrs.get("CurrentIPv4.1.Gateway", "")

                    dhcp_disabled_value = profile.get("dhcp_enable_flag", "Disabled")
                    if (
                        dhcp_enable == dhcp_disabled_value
                        and current_ip == verify_ip
                        and (not gateway or current_gw == gateway)
                    ):
                        verified = True
        except requests.exceptions.RequestException as exc:
            log.warning("Static IP verification failed for %s: %s", verify_ip, exc)

        if verified:
            log.info(
                "Static IP burned and verified for %s/%s via gateway %s",
                verify_ip,
                netmask,
                gateway,
            )
        else:
            log.warning(
                "Static IP not verified for %s: DHCP=%s IP=%s GW=%s; releasing lease anyway because PATCH returned 2xx",
                verify_ip,
                dhcp_enable,
                current_ip,
                current_gw,
            )

        return EXIT_RELEASE, ""
    except requests.exceptions.RequestException as exc:
        if static_ip_patched:
            return EXIT_RELEASE, f"Error after static IP PATCH: {exc}"
        return EXIT_FAIL, f"Static IP PATCH failed: {exc}"
    except Exception as exc:
        if static_ip_patched:
            return EXIT_RELEASE, f"Unexpected error after static IP PATCH: {exc}"
        return EXIT_FAIL, f"Unexpected error during static IP PATCH: {exc}"


def process_lease(
    temp_ip: str,
    cidr: str,
    inventory: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[int, str]:
    """Probe and configure a single lease.

    Returns (exit_code, message) where exit_code is one of EXIT_SKIP, EXIT_FAIL,
    or EXIT_RELEASE.
    """
    bmc_user = config.get("bmc_username", "root")
    bmc_pass = config.get("bmc_password", "calvin")
    subnets = config.get("subnets", [])
    retries = int(config.get("redfish_retries", 0))
    timeout = int(config.get("redfish_timeout", 30))
    profile = get_vendor_profile(config)

    if not _is_valid_ipv4(temp_ip):
        return EXIT_FAIL, f"Invalid temp IP: {temp_ip}"

    # Fast-path: skip Redfish probe if the lease CIDR is not a known BMC subnet.
    # This prevents unnecessary connection attempts against compute nodes on the
    # admin subnet when the on_lease_script fires for all new leases.
    # Comparison is done via ipaddress.ip_network to tolerate minor formatting
    # differences (e.g. leading zeros, equivalent prefix notation).
    bmc_subnets = config.get("bmc_subnets", [])
    if bmc_subnets and cidr:
        try:
            lease_net = ipaddress.ip_network(cidr, strict=False)
            if not any(lease_net == ipaddress.ip_network(s, strict=False) for s in bmc_subnets):
                log.debug("Lease CIDR %s is not a BMC subnet; skipping", cidr)
                return EXIT_SKIP, f"lease CIDR {cidr} is not a BMC subnet; skipping"
        except ValueError:
            pass  # malformed CIDR — fall through to Redfish probe

    log.info("Processing lease for temp IP %s", temp_ip)

    try:
        service_tag = get_service_tag(temp_ip, bmc_user, bmc_pass, profile, timeout=timeout, retries=retries)
    except requests.exceptions.RequestException as exc:
        return EXIT_FAIL, f"Redfish probe failed for {temp_ip}: {exc}"

    if not service_tag:
        # No Redfish response; this is probably a normal node, not a BMC.
        return EXIT_SKIP, f"no Redfish service tag found at {temp_ip}; not a BMC"

    by_service_tag = {e["SERVICE_TAG"].upper(): e for e in inventory if e.get("SERVICE_TAG")}
    entry = by_service_tag.get(service_tag.upper())
    if not entry:
        return EXIT_SKIP, f"service tag {service_tag} not found in inventory"

    exit_code, msg = configure_bmc(
        entry, temp_ip, bmc_user, bmc_pass, subnets, profile,
        timeout=timeout, retries=retries, expected_tag=service_tag,
    )
    return exit_code, msg


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="BMC lease handler for CoreDHCP")
    parser.add_argument("--mac", required=True, help="MAC address of the DHCP client")
    parser.add_argument("--ip", required=True, help="Temporary IP address assigned by DHCP")
    parser.add_argument("--giaddr", default="", help="Gateway IP address from DHCP relay")
    parser.add_argument("--cidr", default="", help="Subnet CIDR from which the IP was allocated")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to BMC handler config")
    args = parser.parse_args()

    try:
        config = load_config(args.config)

        log_level = config.get("log_level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s: %(message)s",
            stream=sys.stdout,
        )
        log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

        log.info(
            "Lease event: mac=%s ip=%s giaddr=%s cidr=%s",
            args.mac,
            args.ip,
            args.giaddr,
            args.cidr,
        )

        inventory_csv = config.get("inventory_csv", "/config/admin_complete_inventory.csv")
        if not os.path.exists(inventory_csv):
            log.error("Inventory CSV not found: %s", inventory_csv)
            return EXIT_FAIL

        inventory = load_inventory(inventory_csv)
        exit_code, msg = process_lease(args.ip, args.cidr, inventory, config)

        if exit_code == EXIT_FAIL:
            log.error("BMC configuration failed for %s: %s", args.ip, msg)
        elif exit_code == EXIT_SKIP:
            if msg:
                log.info("BMC configuration skipped for %s: %s", args.ip, msg)
            else:
                log.info("BMC configuration skipped for %s", args.ip)
        elif exit_code == EXIT_RELEASE:
            if msg:
                log.info("BMC configured and releasing lease for %s: %s", args.ip, msg)
            else:
                log.info("BMC configured successfully for %s; releasing temp lease", args.ip)

        return exit_code
    except Exception:
        log.exception("Unhandled exception in BMC lease handler")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
