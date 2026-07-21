# Prediction

A regime detector that flags when a server is entering a spiky power regime, so
the governor can pre-engage.

The scope is deliberate. A power spike's onset is a near-instant step with no
leading structure in the power signal itself, so it cannot be forecast at these
sample rates. What is predictable — from upstream OS-scheduler signals
(run-queue jerk, memory deltas) combined with the current power regime — is that
a burst is imminent a few seconds out. The detector targets that, not the exact
onset.

```
core/                        telemetry I/O, causal feature construction,
                             spike labels, onset detection
detectors/random_forest/     scorer.py (inference on the host from a synced
                             model), trainer.py (off-host champion-challenger
                             training), and the C port (native/rf_predict.c)
models/kalman/               an adaptive Kalman filter used as a bounded
                             denoiser feeding level/slope features, plus its
                             C port
deploy/                      systemd user unit for the scorer
```

The scorer writes a risk flag that the slew governor
(`../mitigation/slew-governor/`) consumes.

This is reference only: it ran on the instrumented server against a live
InfluxDB and a trained model, and is not runnable standalone here.
