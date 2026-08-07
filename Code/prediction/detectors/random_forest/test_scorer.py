#!/usr/bin/env python3
"""Regression test: write_prediction must use a SYNCHRONOUS write API.

The batching default (what you get if write_options isn't passed) spawns a
background thread + socket per call that client.close() never reaps -- this
leaked 1022 fds on mycroft and silenced the scorer after ~3h (2026-07-08).
"""
from unittest.mock import MagicMock

from influxdb_client.client.write_api import WriteType

from detectors.random_forest import scorer as model_scorer


def test_write_api_is_synchronous():
    client = MagicMock()
    model_scorer.write_prediction(
        client,
        {"spike_proba": 0.0, "spike_risk": 0, "model_threshold": 0.1,
         "model_version": "test"},
        measurement="test_measurement",
    )
    assert client.write_api.called, "write_api never created"
    opts = client.write_api.call_args.kwargs.get("write_options")
    assert opts is not None, "write_options not passed -> batching default -> fd/thread leak"
    assert opts.write_type == WriteType.synchronous, f"expected synchronous, got {opts.write_type}"


if __name__ == "__main__":
    test_write_api_is_synchronous()
    print("test_model_scorer OK")
