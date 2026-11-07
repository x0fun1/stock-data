"""Exercise actual CLI data loading and study handoffs, not just parser helpers."""
import json
from pathlib import Path

import research

from stock_research.storage.curated_store import CuratedStore


def test_cli_build_preserves_a_comma_separated_stock_panel(tmp_path, capsys):
    from test_end_to_end import runtime_with_prices
    from test_multifactor_engine import protocol
    store, _ = runtime_with_prices(tmp_path)
    spec = protocol()
    spec.update({"horizons": [1], "targets": ["absolute"], "factors": ["mom_60", "rv_20"]})
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert research.main(["build-dataset", "--root", str(tmp_path), "--market", "US", "--tickers", "X,Y",
        "--benchmark", "SPY", "--start", "2024-01-02", "--end", "2024-05-20", "--horizons", "1",
        "--targets", "absolute", "--factors", "mom_60,rv_20", "--protocol", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    panel = store.load("features_labels", result["version"])
    assert set(panel["security_id"]) == {"US:XNAS:X", "US:XNAS:Y"}
    assert panel.groupby("cutoff_date")["security_id"].nunique().eq(2).all()


def test_cli_legacy_evidence_paths_reach_native_study_without_probability_activation(tmp_path, capsys):
    from test_multifactor_engine import historical_panel, protocol
    store = CuratedStore(tmp_path)
    version = store.save("features_labels", historical_panel(n=20, days=140), {"scope": "synthetic_cli_behavior"})
    proof = tmp_path / "synthetic-admission.json"
    proof.write_text(json.dumps({"scope": "synthetic_behavior_only_no_investment_validation"}), encoding="utf-8")
    spec = protocol()
    spec["dataset_admission"] = {"status": "verified", "evidence_paths": [str(proof)]}
    spec["ridge_grid"] = [.1]
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert research.main(["multifactor-study", "--root", str(tmp_path), "--dataset", version, "--market", "US",
        "--horizon", "1", "--target", "absolute", "--factors", "mom_60,ep_ttm", "--models", "ridge",
        "--protocol", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dataset_admission"]["status"] == "verified_point_in_time"
    assert result["dataset_admission"]["admission_validation_scope"] == "declaration_shape_only"
    assert result["activated"] is False
    assert store.catalog.list_models() == []
