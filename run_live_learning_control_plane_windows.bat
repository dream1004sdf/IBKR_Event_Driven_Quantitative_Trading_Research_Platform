@echo off
setlocal
cd /d "%~dp0"
set "TMPDIR="

set "RUN_NAME=M232 Live Learning Control Plane"
set "RESTART_DELAY=10"
set "RUN_COMMAND=.venv\Scripts\python.exe -m recorder.live_learning_control_plane --database-path market_events.db --live-scorer-learning-json exports\m223_live_scorer_learning.json --threshold-candidate-evaluator-json exports\m226_threshold_candidate_evaluator_summary.json --output exports\m232_live_learning_control_plane.md --json-output-path exports\m232_live_learning_control_plane.json --hot-cache-json-path exports\m232_hot_cache.json --reflex-hot-cache-json-path exports\m232_reflex_hot_cache.json --scorer-overlay-cache-json-path exports\m232_scorer_overlay_cache.json --option-structure-cache-json-path exports\m232_option_structure_cache.json --action-gate-cache-json-path exports\m232_action_gate_cache.json --refresh-context-learning --max-ticks 0 --interval-seconds 60 --adaptive --hot-limit 500 --hot-min-interval-seconds 5 --hot-normal-interval-seconds 15 --hot-max-interval-seconds 60 --full-report-interval-seconds 300 --sleep-between-ticks"
call run_with_restart_windows.bat
