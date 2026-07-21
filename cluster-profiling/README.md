# Cluster profiling

An interactive 3D visualization of fleet duty-cycle behavior: which nodes behave
alike, and how a fleet splits between idle-heavy, steady, and bursty machines.
Each node's power CDF becomes a feature vector, k-means groups the nodes, and the
surface is the node-count density over (power threshold, fraction of time below).

```bash
python3 fleet_cdf_viz.py            # synthetic 200-node fleet, serves on :8047
python3 fleet_cdf_viz.py --real     # fleet built from the measured traces
python3 fleet_cdf_viz.py --selfcheck
```

`--real` builds the fleet from the grid-simulation traces
(`../grid-simulation/data/traces/`) as phase-shifted, gain- and noise-jittered
variants of each measured trace, so the clusters correspond to real workload
shapes.

Needs `numpy`, `scipy`, and `plotly`. The generated `index.html` (~13 MB, with
plotly inlined so it works offline) is not checked in.
