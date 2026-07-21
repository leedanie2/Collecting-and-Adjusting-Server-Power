"""
rapl_reader.py
==============
Lightweight RAPL energy-counter utilities for Linux.

Public API
----------
  find_package_zones()  → list[str]   — energy_uj paths for package zones
  RaplMonitor           — background-thread power sampler

RAPL zone selection
-------------------
Only top-level package zones (exactly one colon in the directory name, e.g.
'intel-rapl:0') are used.  Sub-zones such as 'intel-rapl:0:0' (PP0/core) and
'intel-rapl:0:1' (uncore/GT) are children of the package counter and would
double-count their contribution if summed independently.
"""

import os
import time
import threading
from typing import Optional

_POWERCAP = "/sys/class/powercap"


def find_package_zones() -> list[str]:
    """Return sorted list of energy_uj paths for package-level RAPL zones."""
    zones: list[str] = []
    try:
        entries = os.listdir(_POWERCAP)
    except OSError:
        return zones
    for entry in sorted(entries):
        if "intel-rapl:" not in entry:
            continue
        if entry.count(":") != 1:          # skip sub-zones
            continue
        path = os.path.join(_POWERCAP, entry, "energy_uj")
        if os.path.exists(path):
            zones.append(path)
    return zones


def _read_max_range_uj(energy_path: str) -> Optional[int]:
    """Read the wraparound ceiling for a zone's energy_uj counter."""
    range_path = os.path.join(os.path.dirname(energy_path), "max_energy_range_uj")
    try:
        with open(range_path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def _read_uj_each(paths: list[str]) -> Optional[list[int]]:
    """Read energy_uj from each given path; returns None on any read error."""
    values: list[int] = []
    for p in paths:
        try:
            with open(p) as f:
                values.append(int(f.read()))
        except (OSError, ValueError):
            return None
    return values


class RaplMonitor:
    """
    Samples RAPL package power in a daemon thread, edge-aligned to the
    hardware counter's own update instants.

    Edge-aligned sampling (v2 — beat-noise fix)
    -------------------------------------------
    The RAPL energy counter is not continuous: platform firmware advances
    it in chunks every ~20-50 ms on its own clock. The old implementation
    computed ``energy_delta / wall_clock_dt`` between our fixed-interval
    reads — so a poll interval that happened to straddle two firmware
    updates read ~2x true power, and one that caught none read far below
    it. Against a ~25 ms poll that beat pattern produced the +-150 W
    single-sample spikes (and physically impossible sub-idle readings)
    observed on mycroft.

    Fix: poll fast (every ``_POLL_S``), but only compute power when a
    zone's counter VALUE changes, taking watts between successive change
    instants of that same zone. Numerator and denominator then share the
    hardware's update clock; residual jitter is bounded by the poll
    cadence. Multi-package systems are handled per zone — each zone's
    power is computed against its own update edges.

    Publication cadence (v2.1): mycroft's counters turned out to refresh
    every ~5 ms, so raw edge emission produced ~210 Hz samples whose short
    integration window makes traces look far noisier (each 5 ms sample
    honestly reports 5 ms of real variation). Edge samples are therefore
    accumulated and PUBLISHED every ``interval`` seconds as their mean —
    restoring the original ~25 ms sample cadence and integration
    smoothness, while the underlying edge alignment keeps the beat-noise
    artifact out of every published value.

    Usage::

        mon = RaplMonitor(interval=0.05)
        mon.start()
        ...
        print(mon.current_watts)
        print(mon.snapshot_watts(window=0.4))
        mon.stop()
        samples = mon.all_samples()   # list of (t_relative, watts)
    """

    _POLL_S = 0.002   # s — fast poll to catch counter-update edges promptly

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self._zones    = find_package_zones()
        self._max_range_uj = [_read_max_range_uj(p) for p in self._zones]
        self._lock     = threading.Lock()
        self._samples: list[tuple[float, float]] = []   # (t_rel_s, watts)
        self._power    = 0.0
        self._t0       = time.monotonic()
        self._running  = False
        self._thread:  Optional[threading.Thread] = None

        if not self._zones:
            print("[RaplMonitor] WARNING: no RAPL zones found under "
                  f"{_POWERCAP!r} — all power readings will be 0.0 W.\n"
                  "  Try running as root, or check that the intel_rapl_msr "
                  "kernel module is loaded.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._t0      = time.monotonic()
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="rapl-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        n = len(self._zones)
        # Per-zone edge state: counter value / timestamp at that zone's
        # last observed update, latest per-zone power, and readiness.
        zone_last_uj: list[Optional[int]] = [None] * n
        zone_last_t:  list[float]         = [0.0] * n
        zone_power:   list[float]         = [0.0] * n
        zone_ready:   list[bool]          = [False] * n

        # Edge samples accumulated between publications (see docstring)
        edge_acc: list[float] = []
        last_pub_t = time.monotonic()

        while self._running:
            uj = _read_uj_each(self._zones)
            t  = time.monotonic()
            if uj is not None:
                any_update = False
                for i in range(n):
                    if zone_last_uj[i] is None:
                        zone_last_uj[i] = uj[i]
                        zone_last_t[i]  = t
                        continue
                    if uj[i] == zone_last_uj[i]:
                        continue                # no hardware refresh for this zone yet
                    delta = uj[i] - zone_last_uj[i]
                    if delta < 0:               # counter wrapped since last update
                        max_range = self._max_range_uj[i]
                        if max_range is None:   # can't correct without the ceiling
                            zone_last_uj[i] = uj[i]
                            zone_last_t[i]  = t
                            continue
                        delta += max_range
                    dt = t - zone_last_t[i]
                    if dt > 1e-6:
                        zone_power[i] = delta / 1e6 / dt
                        zone_ready[i] = True
                        any_update    = True
                    zone_last_uj[i] = uj[i]
                    zone_last_t[i]  = t

                if any_update and all(zone_ready):
                    edge_acc.append(sum(zone_power))

                # Publish the mean of accumulated edge samples once per
                # `interval` — 25 ms integration windows built from clean
                # edge-aligned pieces.
                if edge_acc and (t - last_pub_t) >= self.interval:
                    watts = sum(edge_acc) / len(edge_acc)
                    edge_acc.clear()
                    last_pub_t = t
                    with self._lock:
                        self._power = watts
                        self._samples.append((t - self._t0, watts))

            time.sleep(self._POLL_S)

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def current_watts(self) -> float:
        """Most recent single-interval power sample (W)."""
        with self._lock:
            return self._power

    def snapshot_watts(self, window: float = 0.5) -> float:
        """
        Mean power over the last `window` seconds of samples.
        Falls back to the most-recent sample if no samples in window.
        """
        now = time.monotonic() - self._t0
        with self._lock:
            recent = [w for t, w in self._samples if t > now - window]
            fallback = self._power
        return (sum(recent) / len(recent)) if recent else fallback

    def all_samples(self) -> list[tuple[float, float]]:
        """Return a copy of all (t_relative_s, watts) samples collected so far."""
        with self._lock:
            return list(self._samples)
