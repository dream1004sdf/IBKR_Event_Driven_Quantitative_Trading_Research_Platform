# Testing

## Reproduce

```bash
python -m venv .venv
# Activate the environment for your shell.
python -m pip install -r requirements-dev.txt
python examples/offline_demo.py
python -m pytest -q --junitxml=work/test-results.xml
```

The tests use temporary SQLite databases, synthetic records and mocked service adapters. The offline example validates canonical news ingestion, three deduplication layers, observed reaction measurement and persistence after reopening a database.

## Current public snapshot

Validation date: 2026-10-19, America/New_York. A new Windows virtual environment was installed from `requirements-dev.txt` using Python 3.13.14.

- Dependency consistency: `python -m pip check` passed.
- Offline example and `python main.py self-test`: PASS.
- CLI construction: `python main.py --help` passed.
- Focused compatibility and historical-clock checks: 10 passed.
- Full suite: **854 passed**, zero failures, errors or skips, in **231.81 seconds**.

[Environment versions](VALIDATION_ENVIRONMENT.txt) records the installed packages for this local run. The CI workflow targets Windows and Ubuntu with Python 3.11 and 3.13; its run history is separate evidence from the local results.

## Coverage

Tests cover ingestion/deduplication, schema migration, attribution, timestamp provenance, evidence snapshots, forecast evaluation and calibration, replay leakage checks, semantic policies, clone idempotency, market/capital/option gates, dashboard composition and operating-policy interlocks.

The public snapshot includes two small compatibility improvements: optional signal-lineage fields default to empty values for older input objects, and historical-date fixtures explicitly freeze the simulated time. Those tests retain their original assertions instead of relying on the machine's current date.

## Limits

These tests do not prove live brokerage execution, profitability, current market-data entitlements, real-time service uptime or production readiness. Historical private database counts in the research-status document are not reproducible from this public source snapshot alone.

An earlier private-workspace review passed 124 focused tests. That number describes a different test selection and is not the total for this public suite.
