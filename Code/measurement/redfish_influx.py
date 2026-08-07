#!/usr/bin/env python3
"""Poll HPE iLO 5 Redfish telemetry and stream it to InfluxDB.

Builds on oob_telemetry.py's session/polling logic, but walks the full
PowerSupplies/Temperatures/Fans arrays (not just index [0]) and writes
each reading as its own tagged InfluxDB point instead of printing a
single flattened row.
"""

import os
import sys
import time
import socket
import argparse
from pathlib import Path

import requests
import urllib3
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Redfish/BMC configuration ---
# Site-specific: pass --bmc-ip, or set BMC_IP/BMC_USER in the unit file. The
# lab's management address is not baked in here.
BMC_IP = os.environ.get("BMC_IP", "")
BASE_URL = f"https://{BMC_IP}/redfish/v1"
USERNAME = os.environ.get("BMC_USER", "Administrator")
PASSWORD_PATH = Path(
    os.environ.get("BMC_PASSWORD_FILE", Path.home() / ".secrets" / "bmc_password.txt")
)

# --- InfluxDB configuration ---
TOKEN_PATH = Path.home() / ".secrets" / "influx_token.txt"
INFLUX_URL = "http://mycroft:8086"
INFLUX_ORG = "Power"
INFLUX_BUCKET = "Power"

POLL_INTERVAL_S = 10.0
REAUTH_INTERVAL_S = 1200.0  # iLO sessions expire ~30min; refresh well before that


def should_reauth(last_auth_time, now, interval_s=REAUTH_INTERVAL_S):
    return (now - last_auth_time) >= interval_s


def load_token(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        print(f"Error: token file not found at {path}.", file=sys.stderr)
        sys.exit(1)


def create_session():
    """Authenticates and retrieves the X-Auth-Token for iLO 5."""
    url = f"{BASE_URL}/SessionService/Sessions/"
    payload = {"UserName": USERNAME, "Password": load_token(PASSWORD_PATH)}
    response = requests.post(url, json=payload, verify=False)

    if response.status_code in [200, 201]:
        return response.headers.get("X-Auth-Token")
    else:
        raise Exception(f"Failed to authenticate: {response.status_code}")


def get_json(headers, path):
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, verify=False, timeout=5)
    resp.raise_for_status()
    return resp.json()


def collect_points(headers, system):
    """Return a list of InfluxDB Points covering power, thermal, and system telemetry."""
    points = []

    power_resp = get_json(headers, "/Chassis/1/Power")

    for entry in power_resp.get("PowerControl", []):
        watts = entry.get("PowerConsumedWatts")
        if isinstance(watts, (int, float)):
            points.append(
                Point("bmc_power").tag("system", system).field("watts", float(watts))
            )

    for i, psu in enumerate(power_resp.get("PowerSupplies", [])):
        p = Point("bmc_psu").tag("system", system).tag("psu", str(i))
        has_field = False
        voltage = psu.get("LineInputVoltage")
        if isinstance(voltage, (int, float)):
            p.field("input_voltage", float(voltage))
            has_field = True
        health = psu.get("Status", {}).get("Health")
        if health:
            p.field("health", str(health))
            has_field = True
        if has_field:
            points.append(p)

    thermal_resp = get_json(headers, "/Chassis/1/Thermal")

    for temp in thermal_resp.get("Temperatures", []):
        reading = temp.get("ReadingCelsius")
        if isinstance(reading, (int, float)):
            name = temp.get("Name", "unknown")
            points.append(
                Point("bmc_temp")
                .tag("system", system)
                .tag("sensor", name)
                .field("celsius", float(reading))
            )

    for fan in thermal_resp.get("Fans", []):
        name = fan.get("Name", "unknown")
        p = Point("bmc_fan").tag("system", system).tag("fan", name)
        has_field = False
        reading = fan.get("Reading")
        if isinstance(reading, (int, float)):
            p.field("reading", float(reading))
            has_field = True
        health = fan.get("Status", {}).get("Health")
        if health:
            p.field("health", str(health))
            has_field = True
        if has_field:
            points.append(p)

    system_resp = get_json(headers, "/Systems/1")
    sys_point = Point("bmc_system").tag("system", system)
    has_field = False

    mem_gb = system_resp.get("MemorySummary", {}).get("TotalSystemMemoryGiB")
    if isinstance(mem_gb, (int, float)):
        sys_point.field("memory_gb", float(mem_gb))
        has_field = True

    health = system_resp.get("Status", {}).get("Health")
    if health:
        sys_point.field("health", str(health))
        has_field = True

    proc_resp = get_json(headers, "/Systems/1/Processors/1")
    freq = proc_resp.get("OperatingSpeedMHz") or proc_resp.get("BaseSpeedMHz") or proc_resp.get("MaxSpeedMHz")
    if isinstance(freq, (int, float)):
        sys_point.field("cpu_freq_mhz", float(freq))
        has_field = True

    eth_resp = get_json(headers, "/Systems/1/EthernetInterfaces")
    net_links = eth_resp.get("Members@odata.count")
    if isinstance(net_links, (int, float)):
        sys_point.field("net_links", int(net_links))
        has_field = True

    if has_field:
        points.append(sys_point)

    return points


def selfcheck(system):
    """Live-hardware check: auth + one real poll, assert the curated fields
    analysis/common.py depends on are actually present. No cached-data
    fallback exists for Redfish (unlike spike_daemon.py's CSV selfcheck) --
    this hits the real BMC.
    """
    print("Authenticating with BMC...")
    auth_token = create_session()
    headers = {"X-Auth-Token": auth_token}
    points = collect_points(headers, system)
    lines = [p.to_line_protocol() for p in points]

    assert any(l.startswith("bmc_power") for l in lines), "no bmc_power point collected"
    assert any(l.startswith("bmc_psu") for l in lines), "no bmc_psu point collected"
    assert any(l.startswith("bmc_fan") for l in lines), "no bmc_fan point collected"
    assert any("PkgTmp" in l for l in lines), "no CPU package temp sensor found in bmc_temp points"

    print(f"selfcheck OK: {len(points)} points, including CPU package temp + PSU + fan + power")


def main():
    p = argparse.ArgumentParser(description="Stream HPE iLO 5 Redfish telemetry to InfluxDB.")
    p.add_argument("--bmc-ip", default=BMC_IP,
                   help="BMC IP address (or set BMC_IP in the environment).")
    p.add_argument("--system", default=None,
                   help="Tag identifying the polled server (default: BMC IP).")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL_S,
                   help="Poll interval in seconds (default: %(default)s).")
    p.add_argument("--selfcheck", action="store_true",
                   help="Authenticate, collect one round of points, assert curated fields are present, then exit.")
    args = p.parse_args()

    if not args.bmc_ip:
        p.error("no BMC address: pass --bmc-ip or set BMC_IP in the environment")
    if args.system is None:
        args.system = args.bmc_ip

    global BASE_URL
    BASE_URL = f"https://{args.bmc_ip}/redfish/v1"

    if args.selfcheck:
        selfcheck(args.system)
        return

    token = load_token(TOKEN_PATH)
    client = InfluxDBClient(url=INFLUX_URL, token=token, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    print("Authenticating with BMC...")
    auth_token = create_session()
    headers = {"X-Auth-Token": auth_token}
    last_auth = time.monotonic()

    print(f"[redfish_influx] system={args.system} interval={args.interval}s "
          f"-> {INFLUX_URL} org={INFLUX_ORG} bucket={INFLUX_BUCKET}. Ctrl+C to stop.")

    try:
        while True:
            try:
                now = time.monotonic()
                if should_reauth(last_auth, now):
                    print(f"[{time.ctime()}] Refreshing BMC session...")
                    auth_token = create_session()
                    headers = {"X-Auth-Token": auth_token}
                    last_auth = now

                points = collect_points(headers, args.system)
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
                print(f"[{time.ctime()}] Sent {len(points)} points.")
            except Exception as e:
                print(f"[{time.ctime()}] Error: {e}", file=sys.stderr)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[redfish_influx] stopped.")
    finally:
        write_api.close()
        client.close()


if __name__ == "__main__":
    main()
