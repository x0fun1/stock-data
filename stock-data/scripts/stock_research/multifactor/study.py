"""Local historical multi-factor model comparison with sealed holdouts.

Model selection and factor-family ablations use development data.  A selected
model and fixed controls are refitted on train + development, saved, and used
to seal holdout forecasts before outcomes are evaluated.  Nothing here
activates the existing investment-probability registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..common.hashing import canonical_hash, sha256_file, sha256_text
from ..common.jsonio import write_json
from ..common.timeutil import now_utc
from ..features.registry import MACRO_FACTORS
from ..models.preprocessing import FeatureTransformer
from .catalog import FACTOR_DIRECTIONS, factor_group, group_equal_weight_score
from .diagnostics import evaluate_predictions, training_correlation_clusters
from .preprocessing import confirmed, prepare_cross_section, raw_factor

ENGINE_VERSION = "multifactor-native-v1"
_BLOCKS = ("train", "dev", "cal", "test")
_MODEL_NAMES = {"equal_weight", "ridge", "lightgbm"}
_PROHIBITED_FACTORS = {"label", "label_excess", "return_value", "excess_return", "benchmark_return", "target", "end_date", "matured_at"}


def _date(value, label: str):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError(f"invalid {label}")
    return stamp.date()


def _boundaries(protocol: Mapping[str, Any]) -> dict:
    bounds = {name: (_date(protocol[f"{name}_start"], f"{name}_start"),
                     _date(protocol[f"{name}_end"], f"{name}_end")) for name in _BLOCKS}
    previous_end = None
    for name in _BLOCKS:
        start, end = bounds[name]
        if start > end or (previous_end is not None and start <= previous_end):
            raise ValueError("train/dev/cal/test blocks must be ordered and non-overlapping")
        previous_end = end
    embargo = int(protocol.get("embargo_days", 1))
    if embargo < 0:
        raise ValueError("embargo_days must be nonnegative")
    return {"blocks": bounds, "embargo_days": embargo}


def _one_value(frame: pd.DataFrame, column: str, default):
    if column not in frame:
        return default
    values = frame[column].dropna().astype(str).unique()
    if len(values) != 1 or frame[column].isna().any():
        raise ValueError(f"a study must have exactly one {column}")
    return values[0]


def _causal_frame(frame: pd.DataFrame, protocol: Mapping[str, Any]) -> tuple[pd.DataFrame, dict]:
    required = {"security_id", "cutoff_date", "entry_date", "end_date", "matured_at"}
    missing = required - set(frame)
    if missing:
        raise ValueError(f"historical study requires {sorted(missing)}")
    if frame.empty:
        raise ValueError("historical study requires rows")
    out = frame.copy().reset_index(drop=True)
    if out["security_id"].isna().any() or (out["security_id"].astype(str).str.strip() == "").any():
        raise ValueError("security_id must be nonempty")
    out["security_id"] = out["security_id"].astype(str)
    cutoff = pd.to_datetime(out["cutoff_date"], errors="coerce")
    if cutoff.isna().any() or (cutoff.dt.normalize() != cutoff).any():
        raise ValueError("daily factor study requires valid date cutoffs; hourly research uses a separate contract")
    out["cutoff_date"] = cutoff.dt.date.astype(str)
    if out.duplicated(["cutoff_date", "security_id"]).any():
        raise ValueError("duplicate security/date in historical study")
    market = str(_one_value(out, "market", protocol.get("market", "US")))
    target_default = protocol.get("target") or (protocol.get("targets") or ["absolute"])[0]
    target = str(_one_value(out, "target", target_default))
    if target not in {"absolute", "excess"}:
        raise ValueError("multi-factor return study target must be absolute or excess")
    if protocol.get("market") and str(protocol["market"]) != market:
        raise ValueError("protocol market disagrees with dataset")
    if protocol.get("targets") and target not in protocol["targets"]:
        raise ValueError("protocol does not include dataset target")
    if protocol.get("target") and target != protocol["target"]:
        raise ValueError("protocol target disagrees with dataset")
    horizon_default = protocol.get("horizon") or (protocol.get("horizons") or [1])[0]
    horizon_value = float(_one_value(out, "horizon", horizon_default))
    if not np.isfinite(horizon_value) or horizon_value != int(horizon_value):
        raise ValueError("horizon must be a positive integer")
    horizon = int(horizon_value)
    if horizon < 1 or (protocol.get("horizons") and horizon not in protocol["horizons"]):
        raise ValueError("invalid or unregistered horizon")
    out["market"], out["target"], out["horizon"] = market, target, horizon
    target_col = "excess_return" if target == "excess" else "return_value"
    if target_col not in out:
        raise ValueError(f"dataset has no {target_col}")
    out[target_col] = pd.to_numeric(out[target_col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    entry = pd.to_datetime(out["entry_date"], errors="coerce")
    end = pd.to_datetime(out["end_date"], errors="coerce")
    matured = pd.to_datetime(out["matured_at"], errors="coerce", utc=True)
    if ((entry.notna() & (entry <= cutoff)) | (end.notna() & entry.notna() & (end < entry))).any():
        raise ValueError("labels must begin after feature cutoff and end at or after entry")
    if ((entry.notna() & (entry != entry.dt.normalize())) | (end.notna() & (end != end.dt.normalize()))).any():
        raise ValueError("daily label endpoints must be dates")
    end_utc = pd.to_datetime(out["end_date"], errors="coerce", utc=True)
    if (matured.notna() & end_utc.notna() & (matured < end_utc)).any():
        raise ValueError("matured_at precedes the label end")
    if "feature_cutoff_at" in out:
        feature_at = pd.to_datetime(out["feature_cutoff_at"], errors="coerce", utc=True)
        if feature_at.isna().any() or (feature_at.dt.date.astype(str) != out["cutoff_date"]).any():
            raise ValueError("feature_cutoff_at must refer to its cutoff_date")
        if "feature_available_at" in out:
            available = pd.to_datetime(out["feature_available_at"], errors="coerce", utc=True)
            if available.isna().any() or (available > feature_at).any():
                raise ValueError("a feature is unavailable at the requested cutoff")
    elif "feature_available_at" in out:
        raise ValueError("feature_available_at requires feature_cutoff_at")
    settings = _boundaries(protocol)
    now = pd.Timestamp(now_utc())
    raw_partition = pd.Series("excluded", index=out.index, dtype=object)
    eligible = pd.Series(False, index=out.index)
    reasons = pd.Series("outside_protocol", index=out.index, dtype=object)
    all_label_valid = entry.notna() & end.notna() & matured.notna() & out[target_col].notna() & (matured <= now)
    if "label_status" in out:
        all_label_valid &= out["label_status"] == "ok"
    else:
        out["label_status"] = np.where(all_label_valid, "ok", "missing")
    for pos, name in enumerate(_BLOCKS):
        start, finish = settings["blocks"][name]
        selected = (cutoff.dt.date >= start) & (cutoff.dt.date <= finish)
        raw_partition.loc[selected] = name
        allowed = all_label_valid.copy()
        if name == "test":
            allowed &= (end.dt.date <= finish) & (matured <= pd.Timestamp(finish, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))
        else:
            next_start = settings["blocks"][_BLOCKS[pos + 1]][0] - timedelta(days=settings["embargo_days"])
            allowed &= (end.dt.date < next_start) & (matured < pd.Timestamp(next_start, tz="UTC"))
        eligible.loc[selected] = allowed.loc[selected]
        reasons.loc[selected] = np.where(allowed.loc[selected], "", "unavailable_or_boundary_crossing_label")
    out["partition"] = raw_partition
    out["evaluation_eligible"] = eligible
    out["exclusion_reason"] = reasons
    return out.sort_values(["cutoff_date", "security_id"], kind="stable").reset_index(drop=True), {
        "market": market, "target": target, "horizon": horizon, "target_column": target_col,
        "partition_counts": {name: int((raw_partition == name).sum()) for name in _BLOCKS},
        "eligible_counts": {name: int(((raw_partition == name) & eligible).sum()) for name in _BLOCKS},
        "purged_or_unavailable_rows": int(((raw_partition != "excluded") & ~eligible).sum()),
        "partition_rule": "date-grouped, actual label end and maturity before next block minus embargo",
        "embargo_days": settings["embargo_days"],
    }


_LOCATOR_FIELDS = {
    "path", "paths", "file", "files", "uri", "url", "locator", "locators",
    "evidence_path", "evidence_paths", "evidence_locator", "evidence_locators",
    "manifest", "manifest_path", "report_path", "receipt_path", "audit_path",
}


def _declared_locators(value: Any) -> list[str]:
    """Accept strings and typed/nested locator structures, never boolean flags."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [locator for item in value for locator in _declared_locators(item)]
    if isinstance(value, Mapping):
        result = []
        for key, item in value.items():
            if str(key).lower() in _LOCATOR_FIELDS:
                result.extend(_declared_locators(item))
            elif isinstance(item, Mapping):
                # Supports per-channel structures such as {prices: {path: ...}}.
                result.extend(_declared_locators(item))
            elif isinstance(item, (list, tuple)):
                # Metadata lists such as {notes: ['downloaded']} are not paths;
                # unknown fields must contain explicitly typed locator objects.
                result.extend(locator for nested in item if isinstance(nested, Mapping)
                              for locator in _declared_locators(nested))
        return result
    return []


def normalize_dataset_admission(value: Any) -> dict:
    """Canonicalize a dataset-admission declaration for both CLI and engine.

    Pass ``protocol.get('dataset_admission')``, not the full protocol.  Evidence
    may be a nonempty string, a typed/nested locator structure, or legacy
    ``evidence_paths``.  Empty structures and confirmation flags are not
    evidence.  ``verified`` canonicalizes to ``verified_point_in_time`` only
    with at least one locator; otherwise it becomes unverified research.

    This helper validates the declaration's shape.  It does not read a path or
    establish that the referenced data passed PIT/validation checks.  Keeping
    that distinction avoids treating a protocol claim as probability evidence.
    """
    if not isinstance(value, Mapping):
        return {"status": "research_development_unverified", "reason": "no_point_in_time_admission_manifest",
                "has_evidence_locator": False, "evidence_locators": [],
                "admission_validation_scope": "declaration_shape_only"}
    out = dict(value)
    status_value = out.get("status")
    declared_status = status_value.strip().lower() if isinstance(status_value, str) else "research_development_unverified"
    if not declared_status:
        declared_status = "research_development_unverified"
    locators = []
    for field in ("evidence", "evidence_paths", "evidence_locators"):
        locators.extend(_declared_locators(out.get(field)))
    locators = list(dict.fromkeys(locators))
    status = declared_status
    if status in {"verified", "verified_point_in_time"}:
        if locators:
            status = "verified_point_in_time"
        else:
            status = "research_development_unverified"
            out["reason"] = "admission_has_no_evidence_locator"
    out.update({"status": status, "declared_status": out.get("declared_status", declared_status),
                "has_evidence_locator": bool(locators), "evidence_locators": locators,
                "admission_validation_scope": "declaration_shape_only"})
    return out


def _admission(protocol: Mapping[str, Any]) -> dict:
    return normalize_dataset_admission(protocol.get("dataset_admission"))


def _kronos_gate(frame: pd.DataFrame, protocol: Mapping[str, Any], factors: Sequence[str]) -> None:
    if not any(name.startswith("kronos_") for name in factors):
        return
    manifest = protocol.get("kronos_oof_manifest")
    if not isinstance(manifest, Mapping) or not all(confirmed(manifest.get(key)) for key in ("sealed", "out_of_fold", "frozen_model")) or not _declared_locators(manifest.get("evidence")):
        raise ValueError("Kronos factors require a sealed, frozen, out-of-fold evidence manifest")
    if manifest.get("forecast_mode", "forward_asof") != "forward_asof":
        raise ValueError("Kronos retrospective masked/OOF feature export is not implemented; retain actual sealing times instead of backdating them")
    columns = {"kronos_input_cutoff_at", "kronos_forecast_sealed_at", "kronos_model_trained_through"}
    if not columns.issubset(frame):
        raise ValueError("Kronos factors require row-level causal forecast timestamps")
    cutoff = pd.to_datetime(frame["feature_cutoff_at"], errors="coerce", utc=True) if "feature_cutoff_at" in frame else pd.to_datetime(frame["cutoff_date"], utc=True) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    for name in columns:
        times = pd.to_datetime(frame[name], errors="coerce", utc=True)
        if name == "kronos_forecast_sealed_at" and (times > cutoff).any():
            raise ValueError("Kronos forecast was sealed after the feature cutoff; this gate requires recorded forward-asof evidence, and retrospective masked/OOF export is not implemented; do not backdate the seal")
        if times.isna().any() or (times > cutoff).any():
            raise ValueError("Kronos forecast or training history is not causal at a feature cutoff")


def _date_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame["cutoff_date"].astype(str).value_counts()
    weights = frame["cutoff_date"].astype(str).map(lambda day: 1.0 / counts[day]).to_numpy(dtype=float)
    return weights / weights.sum()


def _sanitize(frame: pd.DataFrame, factors: Sequence[str]) -> pd.DataFrame:
    out = frame.copy()
    for name in factors:
        values = raw_factor(out, name)
        out["f__" + name] = values
        out["m__" + name] = values.isna().astype(int)
    return out


@dataclass
class FittedCandidate:
    name: str
    transformer: FeatureTransformer | None
    factors: list[str]
    parameters: dict
    state: dict
    booster: Any = None

    def _matrix(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        cleaned = _sanitize(frame, self.factors)
        valid = pd.DataFrame({name: raw_factor(cleaned, name).notna() for name in self.factors}).any(axis=1).to_numpy()
        return self.transformer.transform(cleaned), valid

    def _equal_score(self, frame: pd.DataFrame) -> pd.Series:
        cleaned = _sanitize(frame, self.factors)
        X = self.transformer.transform(cleaned)
        names = self.transformer.output_names
        z = pd.DataFrame(index=frame.index)
        for name in self.factors:
            if name in names:
                values = pd.Series(X[:, names.index(name)], index=frame.index)
                z["z__" + name] = values.where(raw_factor(cleaned, name).notna())
        return group_equal_weight_score(z, self.factors, self.parameters.get("directions"),
                                        min_groups=int(self.parameters.get("min_groups", 2)),
                                        groups=self.parameters.get("groups"),
                                        require_complete=bool(self.parameters.get("require_complete_family_coverage", True)))

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if self.name == "mean":
            return np.full(len(frame), float(self.state["intercept"]), dtype=float)
        X, valid = self._matrix(frame)
        if self.name == "equal_weight":
            score = self._equal_score(frame).to_numpy(dtype=float)
            result = float(self.state["intercept"]) + float(self.state["slope"]) * score
            valid = valid & np.isfinite(score)
        elif self.name == "ridge":
            result = float(self.state["intercept"]) + X @ np.asarray(self.state["coefficients"], dtype=float)
        elif self.name == "lightgbm":
            result = np.asarray(self.booster.predict(X), dtype=float)
        else:
            raise ValueError("unknown fitted candidate")
        return np.where(valid, result, np.nan)

    def to_dict(self) -> dict:
        return {"engine_version": ENGINE_VERSION, "model_type": self.name,
                "factors": self.factors, "parameters": self.parameters,
                "transformer": self.transformer.to_dict() if self.transformer else None,
                "state": self.state,
                "prediction_kind": "research_return_score_not_calibrated_probability"}


def _fit_candidate(
    name: str,
    frame: pd.DataFrame,
    factors: Sequence[str],
    target_col: str,
    parameters: Mapping[str, Any],
    seed: int,
) -> FittedCandidate:
    y = frame[target_col].to_numpy(dtype=float)
    w = _date_weights(frame)
    if name == "mean":
        return FittedCandidate(name, None, [], dict(parameters), {"intercept": float(w @ y)})
    cleaned = _sanitize(frame, factors)
    transformer = FeatureTransformer().fit(cleaned, factors)
    # Keep an explicit missing indicator even if missingness first appears later.
    for feature in transformer.params.features:
        feature["indicator"] = True
    usable = [feature["name"] for feature in transformer.params.features]
    X = transformer.transform(cleaned)
    model = FittedCandidate(name, transformer, usable, dict(parameters), {})
    if name == "equal_weight":
        score = model._equal_score(frame).to_numpy(dtype=float)
        valid = np.isfinite(score)
        if int(valid.sum()) < 3:
            raise ValueError("equal-weight control has insufficient signed factor-family coverage")
        ws, ys, s = w[valid] / w[valid].sum(), y[valid], score[valid]
        s_mean, y_mean = float(ws @ s), float(ws @ ys)
        variance = float(ws @ ((s - s_mean) ** 2))
        if variance <= 1e-14:
            raise ValueError("equal-weight score has no variation")
        slope = max(0.0, float(ws @ ((s - s_mean) * (ys - y_mean))) / variance)
        model.state = {"slope": slope, "intercept": y_mean - slope * s_mean,
                       "return_scale_rule": "nonnegative least-squares slope fitted on fitting block only"}
    elif name == "ridge":
        alpha = float(parameters.get("alpha", 0.1))
        if not np.isfinite(alpha) or alpha <= 0:
            raise ValueError("ridge alpha must be positive")
        x_mean, y_mean = w @ X, float(w @ y)
        xc, yc = X - x_mean, y - y_mean
        matrix = xc.T @ (w[:, None] * xc) + alpha * np.eye(X.shape[1])
        rhs = xc.T @ (w * yc)
        coefficients = np.linalg.solve(matrix, rhs)
        model.state = {"coefficients": coefficients.tolist(), "intercept": float(y_mean - x_mean @ coefficients),
                       "solver": "date-weighted ridge closed form with unpenalized intercept"}
    elif name == "lightgbm":
        import lightgbm as lgb

        rounds = int(parameters.get("num_boost_round", 80))
        if not 1 <= rounds <= 2000:
            raise ValueError("LightGBM num_boost_round must be in 1..2000")
        training_params = {
            "objective": "regression", "metric": "l2", "verbosity": -1,
            "learning_rate": float(parameters.get("learning_rate", 0.05)),
            "max_depth": int(parameters.get("max_depth", 4)),
            "num_leaves": int(parameters.get("num_leaves", 15)),
            "min_data_in_leaf": int(parameters.get("min_data_in_leaf", 20)),
            "lambda_l2": float(parameters.get("lambda_l2", 1.0)),
            "seed": seed, "deterministic": True, "force_col_wise": True,
            "num_threads": 2,
        }
        dataset = lgb.Dataset(X, label=y, weight=w * len(w), free_raw_data=False)
        model.booster = lgb.train(training_params, dataset, num_boost_round=rounds)
        model.state = {"library_version": lgb.__version__, "training_parameters": training_params,
                       "num_boost_round": rounds, "model_text_file": "model-lightgbm.txt"}
    else:
        raise ValueError("unsupported multi-factor model")
    return model


def _candidate_grid(name: str, protocol: Mapping[str, Any]) -> list[dict]:
    if name == "equal_weight":
        return [{"directions": {**FACTOR_DIRECTIONS, **dict(protocol.get("factor_directions") or {})},
                 "groups": dict(protocol.get("factor_groups") or {}),
                 "min_groups": int(protocol.get("equal_weight_min_groups", 2)),
                 "require_complete_family_coverage": not confirmed(protocol.get("allow_partial_family_coverage"))}]
    if name == "ridge":
        return [{"alpha": float(alpha)} for alpha in protocol.get("ridge_grid", (0.01, 0.1, 1.0))]
    if name == "lightgbm":
        grid = protocol.get("lightgbm_grid") or [
            {"num_leaves": 7, "max_depth": 3, "num_boost_round": 80},
            {"num_leaves": 15, "max_depth": 4, "num_boost_round": 80},
        ]
        allowed = {"num_leaves", "max_depth", "num_boost_round", "learning_rate", "min_data_in_leaf", "lambda_l2"}
        if not isinstance(grid, (list, tuple)) or any(not isinstance(item, Mapping) or set(item) - allowed for item in grid):
            raise ValueError("LightGBM grid contains unsupported parameters")
        return [dict(item) for item in grid]
    raise ValueError("unknown model candidate")


def _metric_kwargs(protocol: Mapping[str, Any], min_universe: int, seed: int) -> dict:
    return {"min_universe": min_universe, "block_days": int(protocol.get("block_days", 5)),
            "resamples": int(protocol.get("bootstrap_resamples", 250)), "seed": seed,
            "cost_bps": float(protocol.get("cost_bps", 10.0))}


def _selection_score(metrics: Mapping[str, Any], criterion: str) -> float:
    value = metrics.get(criterion)
    if value is None or not np.isfinite(value):
        return float("inf")
    return -float(value) if criterion == "rank_ic_mean" else float(value)


def _save_model(model: FittedCandidate, directory: Path, stem: str) -> dict:
    if model.name == "lightgbm":
        text_path = directory / f"{stem}-lightgbm.txt"
        # LightGBM tree_sizes records LF byte offsets.  Windows newline
        # translation changes those offsets and can abort the native parser.
        with text_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(model.booster.model_to_string())
        model.state["model_text_file"] = text_path.name
        model.state["model_text_sha256"] = sha256_file(text_path)
    path = directory / f"{stem}.json"
    write_json(path, model.to_dict())
    files = {path.name: sha256_file(path)}
    if model.name == "lightgbm":
        files[text_path.name] = sha256_file(text_path)
    return files


def load_candidate(model_path: str | Path, expected_sha256: str | None = None) -> FittedCandidate:
    """Load local JSON/native tree artifacts without pickle or a model service."""
    path = Path(model_path)
    if expected_sha256 is not None and sha256_file(path) != expected_sha256:
        raise ValueError("research model checksum mismatch")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("engine_version") != ENGINE_VERSION or data.get("model_type") not in _MODEL_NAMES | {"mean"}:
        raise ValueError("unsupported research model artifact")
    transformer = FeatureTransformer.from_dict(data["transformer"]) if data.get("transformer") else None
    model = FittedCandidate(data["model_type"], transformer, list(data["factors"]), dict(data["parameters"]), dict(data["state"]))
    if model.name == "ridge":
        coefficients = np.asarray(model.state.get("coefficients"), dtype=float)
        if transformer is None or len(coefficients) != len(transformer.output_names) or not np.isfinite(coefficients).all():
            raise ValueError("invalid ridge coefficients")
    if model.name == "lightgbm":
        import lightgbm as lgb

        text_name = model.state.get("model_text_file", "")
        if not isinstance(text_name, str) or not text_name or Path(text_name).name != text_name or text_name in {".", ".."}:
            raise ValueError("tree model must be a file in the artifact directory")
        text_path = path.parent / text_name
        if not model.state.get("model_text_sha256") or sha256_file(text_path) != model.state["model_text_sha256"]:
            raise ValueError("research tree model checksum mismatch")
        # Python owns filesystem decoding, including non-ASCII Windows paths;
        # the native loader receives the verified text rather than a filename.
        model.booster = lgb.Booster(model_str=text_path.read_text(encoding="utf-8"))
    return model


def run_multifactor_study(
    frame: pd.DataFrame,
    protocol: Mapping[str, Any],
    factors: Sequence[str],
    out_dir: str | Path,
    candidates: Sequence[str] | None = None,
    neutralize: bool = False,
    min_universe: int = 20,
    seed: int = 20261107,
) -> dict:
    """Compare a finite native model budget and seal one historical holdout.

    A holdout ledger under ``out_dir/.holdouts`` refuses repeated evaluation of
    the same market/target/horizon/date/security holdout, including retries with
    altered labels or hyperparameters.  Reading existing artifacts is allowed.
    This local ledger cannot establish that a researcher has never seen dates
    in a different project; the admission manifest must state that limitation.
    """
    if not isinstance(protocol, Mapping):
        raise ValueError("protocol must be a mapping")
    factors = list(factors)
    if not factors or len(set(factors)) != len(factors) or set(factors) & _PROHIBITED_FACTORS:
        raise ValueError("provide unique explanatory factors, not outcome columns")
    if min_universe < 3:
        raise ValueError("min_universe must be at least 3")
    if protocol.get("factors") and list(protocol["factors"]) != factors:
        raise ValueError("factor order must match the registered protocol")
    seed = int(protocol.get("seed", seed))
    candidate_names = list(candidates or protocol.get("multifactor_candidates") or ["equal_weight", "ridge", "lightgbm"])
    if not candidate_names or len(set(candidate_names)) != len(candidate_names) or set(candidate_names) - _MODEL_NAMES:
        raise ValueError("candidates must be unique equal_weight, ridge, or lightgbm")
    criterion = str(protocol.get("selection_metric", "date_weighted_mse"))
    if criterion not in {"date_weighted_mse", "rank_ic_mean"}:
        raise ValueError("selection_metric must be date_weighted_mse or rank_ic_mean")
    coverage_floor = float(protocol.get("dev_min_prediction_coverage", 0.9))
    if not np.isfinite(coverage_floor) or not 0 <= coverage_floor <= 1:
        raise ValueError("dev_min_prediction_coverage must be in 0..1")
    data, temporal = _causal_frame(frame, protocol)
    _kronos_gate(data, protocol, factors)
    unavailable_macro = [name for name in factors if name in MACRO_FACTORS]
    if unavailable_macro:
        if confirmed(protocol.get("macro_as_features")):
            if not confirmed(protocol.get("macro_point_in_time_confirmed")):
                raise ValueError("macro model inputs require an explicit historical-vintage admission")
        else:
            factors = [name for name in factors if name not in MACRO_FACTORS]
    if not factors:
        raise ValueError("no stock-varying factors remain; common macro levels are context by default")
    for name in factors:
        if "f__" + name not in data and name not in data:
            raise ValueError(f"dataset has no explanatory factor {name}")
    if "security_type" in data:
        data = data.loc[~data["security_type"].astype(str).str.lower().str.strip().isin({"etf", "etn", "fund", "index", "mutual_fund"})].copy()
    original_for_digest = data.copy()
    cross_section = None
    if neutralize:
        prepared = prepare_cross_section(data, factors, neutralize=True, min_universe=min_universe)
        data = prepared["frame"]
        cross_section = prepared["diagnostics"]
        for name in factors:
            if name not in MACRO_FACTORS:
                data["f__" + name] = data["n__" + name]
                data["m__" + name] = data["n__" + name].isna().astype(int)
    data = _sanitize(data, factors)
    target_col = temporal["target_column"]
    scheduled = {name: data.loc[data["partition"] == name].copy() for name in _BLOCKS}
    eligible = {name: group.loc[group["evaluation_eligible"]].copy() for name, group in scheduled.items()}
    counts = {name: len(group) for name, group in eligible.items()}
    if any(count == 0 for count in counts.values()):
        raise ValueError(f"empty eligible historical block(s): {counts}")
    clusters = training_correlation_clusters(eligible["train"], factors,
                                             threshold=float(protocol.get("correlation_threshold", 0.8)),
                                             min_pairs=int(protocol.get("correlation_min_pairs", 20)))
    selected_factors = clusters["representatives"]
    columns = sorted(set(["security_id", "cutoff_date", "entry_date", "end_date", "matured_at", "market", "target", "horizon", target_col,
                          "industry", "log_mcap", "industry_pit_confirmed", "size_pit_confirmed", "feature_cutoff_at", "feature_available_at",
                          "kronos_input_cutoff_at", "kronos_forecast_sealed_at", "kronos_model_trained_through"]
                         + factors + ["f__" + name for name in factors] + ["m__" + name for name in factors]) & set(original_for_digest))
    data_digest = sha256_text(original_for_digest[columns].to_csv(index=False, lineterminator="\n"))
    admission = _admission(protocol)
    holdout = {"market": temporal["market"], "target": temporal["target"], "horizon": temporal["horizon"],
               "test_start": str(protocol["test_start"]), "test_end": str(protocol["test_end"]),
               "securities": sorted(scheduled["test"]["security_id"].astype(str).unique().tolist())}
    holdout_digest = canonical_hash(holdout)
    protocol_digest = canonical_hash({**dict(protocol), "effective_factors": factors, "candidates": candidate_names,
                                      "neutralize": neutralize, "min_universe": min_universe, "seed": seed})
    experiment_id = "mf-" + canonical_hash({"protocol": protocol_digest, "data": data_digest})[:16]
    root = Path(out_dir)
    ledger_dir = root / ".holdouts"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    claim_path = ledger_dir / f"{holdout_digest}.json"
    claim = {"status": "reserved_test_not_evaluated", "experiment_id": experiment_id, "holdout": holdout,
             "data_sha256": data_digest, "protocol_sha256": protocol_digest, "created_at": now_utc().isoformat()}
    try:
        with claim_path.open("x", encoding="utf-8") as handle:
            json.dump(claim, handle, ensure_ascii=False, sort_keys=True, indent=2)
    except FileExistsError as exc:
        raise ValueError("this historical holdout is already reserved or evaluated in this research directory; read the sealed artifacts") from exc
    directory = root / experiment_id
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "registered-protocol.json", {"protocol": dict(protocol), "effective_factors": factors,
               "dataset_admission": admission, "data_sha256": data_digest, "holdout_sha256": holdout_digest,
               "protocol_sha256": protocol_digest, "engine_version": ENGINE_VERSION,
               "temporal_checks": temporal, "neutralization": cross_section})
    kwargs = _metric_kwargs(protocol, min_universe, seed)
    attempts, fitted_development = [], []
    try:
        grids = {name: _candidate_grid(name, protocol) for name in candidate_names}
        budget = int(protocol.get("candidate_budget", 16))
        if budget < 1 or sum(len(grid) for grid in grids.values()) > budget:
            raise ValueError("registered model grid exceeds candidate_budget")
        for name in candidate_names:
            for params in grids[name]:
                attempt: dict = {"model_type": name, "parameters": params}
                try:
                    model = _fit_candidate(name, eligible["train"], selected_factors, target_col, params, seed)
                    predicted = model.predict(scheduled["dev"])
                    metrics = evaluate_predictions(scheduled["dev"], predicted, target_col, **kwargs)
                    selection_score = _selection_score(metrics, criterion)
                    prediction_coverage = int(np.isfinite(predicted).sum()) / len(scheduled["dev"])
                    accepted_coverage = prediction_coverage >= coverage_floor
                    if not accepted_coverage:
                        selection_score = float("inf")
                    attempt.update({"status": ("ok" if np.isfinite(selection_score) else "selection_metric_unavailable") if accepted_coverage else "insufficient_development_prediction_coverage", "dev_metrics": metrics,
                                    "dev_prediction_coverage": prediction_coverage, "dev_min_prediction_coverage": coverage_floor,
                                    "usable_factors": model.factors, "selection_score": selection_score if np.isfinite(selection_score) else None})
                    if np.isfinite(selection_score):
                        fitted_development.append((selection_score, len(attempts), model, params, metrics))
                except ImportError as exc:
                    attempt.update({"status": "unavailable", "reason": f"local_dependency_missing:{getattr(exc, 'name', 'lightgbm')}"})
                except Exception as exc:
                    attempt.update({"status": "failed", "reason": str(exc)})
                attempts.append(attempt)
        if not fitted_development:
            result = {"status": "suspended", "reason": "no_candidate_with_usable_development_metric",
                      "experiment_id": experiment_id, "attempts": attempts, "dataset_admission": admission,
                      "prediction_kind": "research_score", "probability_verified": False}
            write_json(directory / "selection.json", result)
            write_json(claim_path, {**claim, "status": "selection_failed_test_not_evaluated", "artifact_directory": str(directory)})
            return result
        fitted_development.sort(key=lambda item: (item[0], item[1]))
        _, _, development_model, selected_params, best_dev_metrics = fitted_development[0]
        chosen = development_model.name
        ablations = []
        groups = sorted({factor_group(name, protocol.get("factor_groups")) for name in selected_factors})
        for omitted in groups:
            retained = [name for name in selected_factors if factor_group(name, protocol.get("factor_groups")) != omitted]
            result = {"omitted_family": omitted, "selection_scope": "development_only", "retained_factors": retained}
            try:
                ablation_name = chosen if retained else "mean"
                ablated = _fit_candidate(ablation_name, eligible["train"], retained, target_col, selected_params, seed)
                metrics = evaluate_predictions(scheduled["dev"], ablated.predict(scheduled["dev"]), target_col, **kwargs)
                result.update({"status": "ok", "dev_metrics": metrics,
                               "delta_date_weighted_mse": metrics["date_weighted_mse"] - best_dev_metrics["date_weighted_mse"]
                               if metrics["date_weighted_mse"] is not None and best_dev_metrics["date_weighted_mse"] is not None else None})
            except (ValueError, ImportError, np.linalg.LinAlgError) as exc:
                result.update({"status": "unavailable", "reason": str(exc)})
            ablations.append(result)
        selection = {"selected_model_type": chosen, "selected_parameters": selected_params,
                     "selection_metric": criterion, "selection_scope": "train_fit_development_selection",
                     "dev_min_prediction_coverage": coverage_floor,
                     "candidate_attempts": attempts, "training_correlation_groups": clusters,
                     "family_ablations": ablations, "calibration_role": "diagnostics_only_no_probability_calibrator",
                     "test_role": "single_sealed_evaluation_after_selection"}
        write_json(directory / "selection.json", selection)
        fit_frame = pd.concat([eligible["train"], eligible["dev"]], ignore_index=True)
        selected = _fit_candidate(chosen, fit_frame, selected_factors, target_col, selected_params, seed)
        mean = _fit_candidate("mean", fit_frame, [], target_col, {}, seed)
        final_models = {"selected": selected, "mean_control": mean}
        if chosen == "equal_weight":
            final_models["equal_weight_control"] = selected
        else:
            try:
                final_models["equal_weight_control"] = _fit_candidate("equal_weight", fit_frame, selected_factors, target_col,
                                                                      _candidate_grid("equal_weight", protocol)[0], seed)
            except (ValueError, np.linalg.LinAlgError):
                pass
        model_hashes = {}
        for label, model in final_models.items():
            model_hashes.update(_save_model(model, directory, label))
        cal_metrics = {label: evaluate_predictions(scheduled["cal"], model.predict(scheduled["cal"]), target_col, **kwargs)
                       for label, model in final_models.items()}
        write_json(directory / "calibration-diagnostics.json", cal_metrics)
        # The forecast file intentionally contains no holdout outcomes.
        predictions = scheduled["test"][["security_id", "market", "cutoff_date", "horizon", "target", "entry_date", "end_date"]].copy()
        coverage_rows = []
        final_input_factors = selected.factors
        declared_families = sorted({factor_group(name, protocol.get("factor_groups")) for name in final_input_factors})
        for _, row in scheduled["test"].iterrows():
            available_factors = [name for name in final_input_factors if pd.notna(row.get("f__" + name)) and row.get("m__" + name, 0) == 0]
            available_families = sorted({factor_group(name, protocol.get("factor_groups")) for name in available_factors})
            coverage_rows.append({"available_factor_count": len(available_factors), "declared_factor_count": len(final_input_factors),
                                  "available_families": "|".join(available_families),
                                  "missing_families": "|".join(name for name in declared_families if name not in available_families)})
        for column in ("available_factor_count", "declared_factor_count", "available_families", "missing_families"):
            predictions[column] = [row[column] for row in coverage_rows]
        for label, model in final_models.items():
            predictions[label + "_score"] = model.predict(scheduled["test"])
        forecasts_path = directory / "test-predictions.csv"
        predictions.to_csv(forecasts_path, index=False, lineterminator="\n")
        seal = {"sealed_at": now_utc().isoformat(), "prediction_file": forecasts_path.name,
                "prediction_sha256": sha256_file(forecasts_path), "model_files": model_hashes,
                "selection_sha256": sha256_file(directory / "selection.json"),
                "protocol_sha256": protocol_digest, "input_sha256": data_digest,
                "contains_holdout_outcomes": False}
        write_json(directory / "forecast-seal.json", seal)
        write_json(claim_path, {**claim, "status": "forecasts_sealed_outcomes_not_evaluated", "artifact_directory": str(directory), "seal": seal})
        test_metrics = {label: evaluate_predictions(scheduled["test"], predictions[label + "_score"].to_numpy(), target_col, **kwargs)
                        for label in final_models}
        outcomes = scheduled["test"][["security_id", "cutoff_date", target_col, "label_status", "matured_at", "evaluation_eligible", "exclusion_reason"]]
        outcomes.to_csv(directory / "test-outcomes.csv", index=False, lineterminator="\n")
        write_json(directory / "test-evaluation.json", test_metrics)
        result = {"status": "complete", "experiment_id": experiment_id, "artifact_directory": str(directory),
                  "selected_model_type": chosen, "selected_parameters": selected_params,
                  "selected_model_usable_factors": selected.factors,
                  "prediction_kind": "historical_research_return_score", "probability_verified": False,
                  "activated": False, "dataset_admission": admission,
                  "evidence_status": "research_only", "selection": selection,
                  "temporal_checks": temporal, "calibration_diagnostics": cal_metrics,
                  "test_metrics": test_metrics, "forecast_seal": seal,
                  "data_sha256": data_digest, "protocol_sha256": protocol_digest,
                  "scope": "cross_sectional" if scheduled["test"].groupby("cutoff_date").size().min() >= min_universe else "limited_universe_time_series",
                  "test_blindness_note": "local seal prevents this directory re-evaluating holdout; prior inspection elsewhere is not ruled out",
                  "macro_policy": "admitted_model_inputs" if confirmed(protocol.get("macro_as_features")) else "context_only",
                  "cost_note": "reported baskets use explicit assumed bps; slippage, borrow, execution and capacity are not validated"}
        write_json(directory / "result.json", result)
        write_json(claim_path, {**claim, "status": "evaluated", "artifact_directory": str(directory),
                               "forecast_sha256": seal["prediction_sha256"], "completed_at": now_utc().isoformat()})
        return result
    except Exception as exc:
        write_json(directory / "failure.json", {"status": "failed", "reason": f"{type(exc).__name__}: {exc}", "attempts": attempts})
        write_json(claim_path, {**claim, "status": "failed_preserved", "artifact_directory": str(directory), "reason": f"{type(exc).__name__}: {exc}"})
        raise
