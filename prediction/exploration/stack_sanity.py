"""Local end-to-end sanity for the detector->flag->capper chain, no network,
no root, no mycroft. Proves: (1) a pickle trained with the physics feature set
round-trips through the production scorer inference path, (2) the scorer's
risk flag drives the real capper process to a (dry-run) cap on fake sysfs.

Run from analysis/: .venv/bin/python -m exploration.stack_sanity
"""

import json
import pickle
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from actuators.rapl_capper import build_fake_tree
from core import features as la
from core import telemetry as common
from detectors.random_forest.continual_detector import train_rf
from detectors.random_forest.live_detector import MIN_THRESHOLD
from detectors.random_forest.scorer import (
    LOOKBACK_FEAT_S, ModelReader, fit_predict_with_fallback,
)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="stack_sanity_"))

    # 1. toy champion with the physics feature set, from cache
    df = pd.read_csv(common.DATA_DIR / "telemetry_procs.csv",
                     index_col="_time", parse_dates=["_time"]).sort_index()
    df = common.weekday_business_hours(df)
    X, y = la.build_xy(df, lane_a=False, kf=True, upstream=True)
    assert set(la.PHYSICS_FEATURE_COLS) <= set(X.columns), "physics cols missing"
    champ = train_rf(X, y)
    pkl = tmp / "spike_model_vstacktest.pkl"
    pkl.write_bytes(pickle.dumps(champ))
    link = tmp / "spike_model_current"
    link.symlink_to(pkl)
    print(f"model OK: {len(champ['cols'])} cols incl. physics, thr={champ['threshold']:.3f}")

    # 2. production scorer inference path against that pickle (offline df)
    res = fit_predict_with_fallback(df.tail(LOOKBACK_FEAT_S + 1), ModelReader(link))
    assert res is not None and res["model_version"] == "vstacktest"
    assert 0.0 <= res["spike_proba"] <= 1.0
    assert res["model_threshold"] >= MIN_THRESHOLD, "production floor not applied"
    print(f"scorer OK: proba={res['spike_proba']:.3f} "
          f"thr={res['model_threshold']:.3f} risk={res['spike_risk']}")

    # 3. risk flag -> real capper process, dry-run, fake powercap tree
    build_fake_tree(tmp / "intel-rapl:0")
    flag = tmp / "spike_risk.flag"
    events = tmp / "events.jsonl"
    common.write_risk_flag(flag, False)
    proc = subprocess.Popen(
        [sys.executable, "-m", "actuators.rapl_capper", "--dry-run",
         "--watch-file", str(flag), "--rapl-root", str(tmp / "intel-rapl:*"),
         "--events", str(events)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(2.0)                       # let it snapshot + start polling
        common.write_risk_flag(flag, True)    # rising edge
        time.sleep(2.0)                       # poll cadence 0.25 s + margin
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    evs = [json.loads(l)["event"] for l in events.read_text().splitlines()] \
        if events.exists() else []
    assert "cap" in evs, f"no cap event; events={evs}\ncapper output:\n{out}"
    print(f"capper OK: flag edge -> events {evs}")
    print("STACK SANITY OK")


if __name__ == "__main__":
    main()
