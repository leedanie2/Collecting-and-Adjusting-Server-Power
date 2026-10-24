# Collecting and Adjusting Server Power

Companion code and data for **Smoothing Synchronized Power Swings in HPC Clusters
Using Software: Measurement, Prediction, and Grid Simulation** (Daniel Lee, Emily
Doyle, Alexander Johnson).

Bulk-synchronous workloads make servers swing power in phase, so software control
of one server matters at fleet scale. This study measures one server, compares three
smoothing methods, and simulates 10,000 identical servers on a weak 50 MW grid
behind a 15 s UPS low-pass filter.

**Poster:** [PDF](Deliverables/Poster/APS-poster-Lee-48x36-v3-mlstyle_proof.pdf) ·
[results and data sources](Code/grid-simulation/POSTER_DATA.md) ·
[earlier paper](Deliverables/Paper/draft.pdf)

## Results

48 measured traces (3 workloads × 4 conditions × 4 repeats) on a dual Xeon Gold 6338
server. Each mitigation is compared with its own workload's baseline.

| Mitigation | Grid variability reduction | Extra runtime | Extra CPU energy |
|---|---:|---:|---:|
| **Ramp & fill** | **91.4 ± 1.4%** | +121.1 ± 5.7% | +161.0 ± 6.7% |
| Power smoother | 8.2 ± 3.5% | +3.5 ± 0.2% | +7.4 ± 0.6% |
| Governor | 21.2 ± 12.7% | +1.1 ± 1.9% | +11.6 ± 1.5% |

Variability is the CV of simulated facility power after the UPS; mean ± sample SD
over four runs, first 30 s excluded. Ramp & fill smooths most at a large cost; the
governor smooths less with little runtime overhead. These are scenario results for
synchronized copies of one measured server.

![Smoothing benefit and runtime/energy cost](Deliverables/Poster/figures/E_tradeoff.png)

**Correction to the earlier paper:** the original Simulink outputs baked in a fixed
initial load and swing-equation operating point. The re-analysis sets these per
trace, so all three methods reduce variability (the paper showed it only for ramp &
fill). [`rescore_warm.py`](Code/grid-simulation/analysis/rescore_warm.py) checks
itself against the original outputs.

**Detection:** forecasting did not give reliable advance warning at the available
telemetry rates (OLS caught 19 of 1,735 spikes; random forest + Kalman had PR-AUC
0.68 but flagged transitions 10–20 s late). The final CPU-utilization edge detector
responds in about one 10 Hz sample; the governor actuated a median 0.23 s after the
transition (33% recall, 19% precision). See [prediction](Code/prediction/README.md).

## Layout

| Directory | Role |
|---|---|
| [Code/measurement](Code/measurement/) | RAPL, system, BMC, and PDU telemetry into InfluxDB |
| [Code/prediction](Code/prediction/) | Forecasting experiments and detector evaluation |
| [Code/mitigation](Code/mitigation/) | Ramp & fill, power smoother, slew governor |
| [Code/grid-simulation](Code/grid-simulation/) | Fleet aggregation, grid model, scoring |
| [Code/workloads](Code/workloads/) | HPL, AI-style, and stepped-load benchmarks |
| [Deliverables](Deliverables/) | Poster, earlier paper, presentations |

## Reproduce the grid results

No MATLAB or instrumented server needed:

```bash
python3 -m pip install -r Code/grid-simulation/requirements.txt
cd Code/grid-simulation
python3 analysis/rescore_warm.py --selfcheck
python3 analysis/rescore_warm.py     # CV, voltage swings, fleet-size sweep
python3 analysis/summarize_n4.py     # runtime/energy
python3 analysis/score_detector.py   # detector evaluation
```

Regenerating the Simulink outputs needs MATLAB R2025a and Simscape Electrical
([guide](Code/grid-simulation/README.md)). Live collection needs the instrumented
Linux server and RAPL permissions. Large telemetry caches are omitted; governor
event logs for the main matrix were not retained.

## Credits and license

Daniel Lee (Grinnell College), Emily Doyle (Embry-Riddle), Alexander Johnson
(Georgia Tech). The AI-style workload, stepped-load generator, and composite ramp
were contributed by collaborators. Done during the 2026 Future Computing Summer
Internship at the National Security Agency; AI tools assisted testing and
implementation. MIT — see [LICENSE](LICENSE).
