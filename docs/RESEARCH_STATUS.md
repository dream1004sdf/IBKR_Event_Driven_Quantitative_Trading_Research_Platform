# Research status

Public snapshot prepared on 2026-10-19. The latest reviewed historical Codex completion is 2026-07-22. This snapshot contains current source files rather than the private repository's earlier committed state.

## Implemented

- IBKR and external-source collection, source lineage, deduplication and event attribution.
- Structured prediction recording, evaluation metrics, probability calibration and replay audits.
- Historical reaction labels with explicit session, corporate-action and eligibility contracts.
- Event-proxy semantic review, isolated clone workflows and point-in-time candidate ranking.
- Capital/options context, paper-action gates, ticket previews and local reporting.

## Historical data evidence

A read-only review of the private canonical research database counted 91,502 raw news records, 83,669 news event clusters and 16,332 macro reaction labels, including 12,192 strict-eligible macro labels. A separate news research clone contains 745,160 news labels. The underlying news and market-data databases are not distributed here, so those counts cannot be reproduced from a fresh clone alone.

The macro label promotion passed its recorded checks. The full-range news clone remains held for independent semantic review. Mechanically valid labels are not automatically approved training or trading signals.

## Latest primary-proxy experiment

The historical Set A run selected 54 of 400 events and abstained on 346. All selected proxies were IEF for rates-policy events. Only 63 events had complete candidate-family data, and the cache contained daily data for 13 of 23 registry symbols.

The ranker excludes the event session's daily bar and uses earlier completed sessions. Its spread measure is an OHLCV high-low proxy, not an observed bid/ask spread. Exposure and beta features use neutral priors because independent pre-event estimates are unavailable. The result is clone-only research, with canonical application held and Set B sealed.

## Remaining work

1. Fill missing pre-event candidate data and rerun the fixed ranker.
2. Complete independent semantic review and resolve unreviewed fields.
3. Connect the latest semantic/proxy decisions to direction calibration and observed reactions.
4. Rebuild and audit the full-range clone under frozen rules.
5. Complete held-out evaluation, purged walk-forward research and after-cost validation.
6. Validate any future order/fill/position lifecycle separately before claiming live automation.

## Lumibot experiment

A 100-row historical Research Agent comparison recorded 31 direction hits for the Lumibot challenger, versus 36 and 38 for two project scorer versions. The active Lumibot bridge/sandbox was subsequently retired. These are research-sample results, not evidence of a profitable strategy.

The retained `VERSION` file describes an older milestone. Later source and research work are described above rather than implied by that marker.
