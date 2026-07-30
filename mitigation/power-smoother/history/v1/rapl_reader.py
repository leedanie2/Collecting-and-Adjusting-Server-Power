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
        # Select by zone NAME, not index. 'intel-rapl:1' is 'psys' (whole-platform,
        # already includes package-0) on many single-socket boxes, not a 2nd socket;
        # summing it double-counts package power. Only 'package-N' zones are real
        # per-socket CPU counters.
        try:
            with open(os.path.join(_POWERCAP, entry, "name")) as f:
                if not f.read().strip().startswith("package-"):
                    continue
        except OSError:
            continue
        path = os.path.join(_POWERCAP, entry, "energy_uj")
        if os.path.exists(path):
            zones.append(path)
    return zones


def _read_total_uj(paths: list[str]) -> Optional[int]:
    """Sum energy_uj across all given paths; returns None on any read error."""
    total = 0
    for p in paths:
        try:
            with open(p) as f:
                total += int(f.read())
        except (OSError, ValueError):
            return None
    return total


class RaplMonitor:
    """
    Samples RAPL package power at a fixed interval in a daemon thread.

    Usage::

        mon = RaplMonitor(interval=0.05)
        mon.start()
        ...
        print(mon.current_watts)
        print(mon.snapshot_watts(window=0.4))
        mon.stop()
        samples = mon.all_samples()   # list of (t_relative_s, watts)
    """

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self._zones    = find_package_zones()
        self._lock     = threading.Lock()
        self._samples: list[tuple[float, float]] = []   # (t_rel_s, watts)
        self._power    = 0.0
        self._t0       = time.monotonic()
        self._last_uj: Optional[int]   = None
        self._last_t:  Optional[float] = None
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
        while self._running:
            uj = _read_total_uj(self._zones)
            t  = time.monotonic()
            if uj is not None and self._last_uj is not None:
                dt = t - self._last_t          # type: ignore[operator]
                if dt > 1e-6:
                    watts = (uj - self._last_uj) / 1e6 / dt
                    with self._lock:
                        self._power = watts
                        self._samples.append((t - self._t0, watts))
            self._last_uj = uj
            self._last_t  = t
            time.sleep(self.interval)

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def current_watts(self) -> float:
        """Most recent single-interval power sample (W)."""
        with self._lock:
            return self._power

    def snapshot_watts(self, window: float = 0.5) -> float:
        """
        Mean power over the last `window` seconds of samples.
        Falls back to the most-recent sample if no samples fell in the window.
        """
        now = time.monotonic() - self._t0
        with self._lock:
            recent   = [w for t, w in self._samples if t > now - window]
            fallback = self._power
        return (sum(recent) / len(recent)) if recent else fallback

    def all_samples(self) -> list[tuple[float, float]]:
        """Return a copy of all (t_relative_s, watts) samples collected."""
        with self._lock:
            return list(self._samples)
