from pathlib import Path

from recorder.analysis_database import AnalysisDatabase
from recorder.s05_reaction_label_pilot import REQUIRED_CONTRACT_COLUMNS, ensure_pilot_schema


def test_s05_label_contract_is_additive_and_idempotent(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "pilot-contract.db")

    with db.connect() as conn:
        ensure_pilot_schema(conn)
        first_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_reaction_labels)")}
        ensure_pilot_schema(conn)
        second_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_reaction_labels)")}

    assert set(REQUIRED_CONTRACT_COLUMNS).issubset(first_columns)
    assert first_columns == second_columns
    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "s05_reaction_label_pilot_runs" in tables
