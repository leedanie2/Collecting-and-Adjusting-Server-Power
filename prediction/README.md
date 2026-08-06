# Prediction

A regime detector that flags when a server is entering a spiky power regime, so
the governor can pre-engage.

The scope is deliberate, and it narrowed over the project. A power spike's onset
is a near-instant step with no leading structure in the power signal itself, so
it cannot be forecast at these sample rates. What is predictable — from upstream
OS-scheduler signals (run-queue jerk, memory deltas) combined with the current
power regime — is that a burst is imminent a few seconds out. The detector
targets that, not the exact onset.

## Layout

```
core/                        telemetry I/O, causal feature construction,
                             spike labels, onset detection
telemetry/                   pull the InfluxDB history down into the local
                             CSV caches everything else trains against
detectors/ols/               the retired OLS forecaster (see below)
detectors/random_forest/     live_detector.py (the on-host daemon),
                             scorer.py (inference from a synced model),
                             trainer.py (off-host champion–challenger
                             training), continual_detector.py (the retired
                             on-box retraining scheme), and the C port
                             (native/rf_predict.c)
models/kalman/               an adaptive Kalman filter used as a bounded
                             denoiser feeding level/slope features, plus its
                             C port
validation/                  evaluation metrics, lead-time diagnostics,
                             stress tests, thermal and HPL feature checks
exploration/                 the screens and one-shot studies the modelling
                             decisions came out of
figures/                     decision-tree schematics
deploy/                      systemd user unit for the scorer
data/                        the model of record and training metrics
```

The scorer writes a risk flag that the slew governor
(`../mitigation/slew-governor/`) consumes.

## What is retired, and why it is still here

The negative results are the reason the final design looks the way it does, so
the code behind them is kept rather than deleted:

- **`detectors/ols/legacy_detector.py`** — the original framing, forecasting a
  continuous power value 5 s out. Least squares minimises global error, which
  is precisely the wrong objective for rare events; it caught almost no spikes.
  This is what motivated reframing the problem as classification.
- **`exploration/regression.py`** — the multivariate fit
  (`power ~ freq + temp + usage`) that showed usage dominates and freq/temp add
  little, which is why the final detector reads usage rather than more channels.
- **`exploration/extended_precursor_screen.py`** — the causal screen over
  candidate upstream heuristics, ranked by Cohen's *d*. Run-queue depth is the
  one signal that genuinely leads a transient.
- **`exploration/confirmation_backtest.py`** — an explicit negative result:
  a fixed-kernel Hawkes intensity over run-queue arrivals, used as
  corroboration rather than prediction, does not beat the power EWMA on either
  latency or false positives. The Hawkes feature is largely redundant with
  run-queue jerk.
- **`detectors/random_forest/continual_detector.py`** — the self-retraining
  challenger scheme. Retraining on-box cost enough CPU to create power spikes
  of its own, which then fed back into the detector's own training data. It was
  replaced by off-host training plus a model sync
  (`scripts/train_and_sync.sh`).
- **`exploration/spectral.py`** — the periodogram that quantifies the workload's
  cyclic structure and the Nyquist floor that bounds achievable lead time.
- **`exploration/characterize_overshoot.py`** — per-onset extraction of the
  PL2→PL1 turbo clamp: burst level, sustained level, overshoot, and the
  measured PL2 window.

The random forest that ultimately shipped was itself retired in favour of
`usage_edge` (in `../mitigation/slew-governor/`), which is two hand-set
thresholds and no training at all. It responds in ~0.1 s against the forest's
10–20 s, holds its flag a fraction as long, and raises far fewer false alarms.
That comparison is the point of keeping both.

An attempt at a lightweight learned Kalman variant is described in the write-up
but left no code worth keeping; the classical adaptive filter in
`models/kalman/` is what was used.

## Running it

Install with `pip install -r requirements.txt`, and run modules from this
directory as a package root:

```bash
python -m core.onsets --selfcheck
python -m detectors.random_forest.test_scorer
python -m models.kalman.kalman_filter --selfcheck
```

Most modules take `--selfcheck`. Six exercise only logic and pass from a clean
clone — `core.onsets`, `models.kalman.kalman_filter`,
`detectors.random_forest.evaluator`, `exploration.characterize_overshoot`,
`exploration.confirmation_backtest`, and the scorer test.

The ones that score against real data need the telemetry caches, which are not
committed — they are hundreds of megabytes and regenerate from InfluxDB:

```bash
python -m telemetry.pull_cache            # data/telemetry.csv
python -m telemetry.pull_process_cache    # data/telemetry_procs.csv
```

Without them those modules stop with a message naming the missing cache rather
than failing obscurely. That path needs the instrumented server's InfluxDB.

## The model of record

`data/models/spike_model_current.forest` is the exported champion — plain-text
tree arrays that the C inference path (`detectors/random_forest/native/rf_predict.c`)
reads directly, 47 features and a calibrated risk threshold in the header. The
dated training pickles it came from are not committed. `data/trainer_metrics.jsonl`
and `data/rf_continual_metrics.jsonl` are the per-refit scores behind the
champion–challenger decisions.
