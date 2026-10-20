# Quantitative trading research architecture

The application implements the data, event-research, validation and paper-decision components of an event-driven quantitative trading system. It follows the research flow from market evidence to candidate forecasts, observed returns, quantitative evaluation and risk-constrained paper actions.

The implementation is modular Python. SQLite holds durable raw, derived and evaluation state; JSON, Markdown and HTML artifacts connect command-line jobs, operating loops and the dashboard.

## Collection and storage

`recorder/ibkr_app.py` implements IBKR `EWrapper`/`EClient` callbacks, subscriptions and reconnect handling. `dedup.py`, `price_cache.py` and `reactions.py` handle article identity, quote snapshots and delayed reaction observations. Historical collectors resolve contracts and track resumable request shards.

`database.py` and `analysis_database.py` provide SQLite storage and additive schema migration. News lineage separates original provider content from delivery routes and derived signals. Provider timestamps, first-seen times and content-update times are retained independently.

## Event interpretation and evidence

`attribution.py` and `news_asset_mapper.py` distinguish the article subject from the delivery ticker. `news_lineage_v2.py`, `macro_event_lineage.py` and event-cluster modules preserve cross-source relationships. Semantic-review modules distinguish causal events from previews, recaps and commentary.

`evidence_packet.py`, `prompting.py` and `analysis_service.py` build versioned inputs before analysis. The analysis queue has queued, processing, retry, complete, skipped and dead states. `analysis_models.py` validates structured outputs, while `ai_provider.py` provides heuristic and optional external-model adapters.

## Evaluation and research

The quantitative research unit is an event/asset/horizon observation. Forecast probabilities and expected reactions are assessed against later market observations, with input availability and outcome eligibility treated as part of the experiment definition.

`prediction_evaluation/metrics.py` calculates classification, probability and return metrics. `calibration.py` implements calibration profiles and time-based splits. Replay engines preserve the prediction cutoff and audit input snapshots for future evidence and outcome leakage.

`reaction_research_engine.py` and S05 research modules associate events with assets and observed reaction windows. Corporate actions, session boundaries, missing trades, overlap groups and eligibility flags are explicit parts of the label contract.

Research runners write to isolated clones, compare hashes and duplicate keys, and repeat builds to check idempotency. Independent semantic review is a separate gate from mechanical data checks.

## Capital, options and presentation

Capital state and action-gate modules combine cash, margin, evidence freshness and market conditions. Option modules build structure context, outcome records and paper-ticket previews. `limited_auto_trading_readiness.py` combines readiness checks.

`ibkr_order_preview.py` and `option_whatif_safety_validation.py` contain broker what-if requests. Those requests use `whatIf=True`; IBKR also requires their API `transmit` flag to be true for calculation. They are distinct from demonstrated live order execution. Default automated refresh policies are disabled.

`trading_dashboard.py` and `trading_dashboard_view.py` assemble the Trading Command Center. A fresh dashboard artifact, an active process and new source data are different states and are assessed independently.

## Useful code entry points

- `main.py` and `recorder/cli_command_registry.py`: command registration and routing.
- `recorder/self_test.py`: synthetic ingestion-to-reaction example.
- `tests/test_prediction_evaluation_engine.py`: evaluation, calibration and leakage cases.
- `tests/test_v014_subject_attribution.py`: subject/delivery attribution cases.
- `tests/test_s05p4_v5_4_1_pit_primary_proxy_ranker.py`: event cutoff, incomplete-data abstention and idempotency.
- `tests/test_execution_safety_cache_supervisor.py`: default broker-refresh interlocks.
