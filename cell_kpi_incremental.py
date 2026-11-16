"""Incremental state: rolling history + small baseline file for next-day compares."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from cell_kpi_trend_engine import (
    TECH_PROFILES,
    build_facts_for_tech,
    panel_from_raw,
)

HISTORY_MAX_DAYS = 90
HISTORY_FILE = "cell_kpi_history.csv"
BASELINE_FILE = "cell_kpi_baseline.csv"
META_FILE = "incremental_meta.json"


def history_path(out_dir: Path) -> Path:
    return out_dir / HISTORY_FILE


def baseline_path(out_dir: Path) -> Path:
    return out_dir / BASELINE_FILE


def panel_to_history_rows(profile: dict, panel: pd.DataFrame) -> pd.DataFrame:
    """Long format: one row per date × DN × kpi_id (compact vs wide PM export)."""
    kid_cols = [k["id"] for k in profile["kpis"]]
    meta = ["DN", "date", "sitecode", "area", "RAN_name", profile["name_col"]]
    meta = [c for c in meta if c in panel.columns]
    rows: list[pd.DataFrame] = []
    for kid in kid_cols:
        if kid not in panel.columns:
            continue
        sub = panel[meta + [kid]].copy()
        sub = sub.rename(columns={kid: "value", profile["name_col"]: "cell_name"})
        sub["tech"] = profile["tech"]
        sub["kpi_id"] = kid
        sub = sub.dropna(subset=["value"])
        sub = sub.reindex(columns=["tech", "date", "DN", "cell_name", "sitecode", "area", "RAN_name", "kpi_id", "value"])
        rows.append(sub)
    if not rows:
        return pd.DataFrame(
            columns=["tech", "date", "DN", "cell_name", "sitecode", "area", "RAN_name", "kpi_id", "value"]
        )
    out = pd.concat(rows, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    return out


def load_history(out_dir: Path, empty: bool = False) -> pd.DataFrame:
    p = history_path(out_dir)
    if empty or not p.is_file():
        return pd.DataFrame(
            columns=["tech", "date", "DN", "cell_name", "sitecode", "area", "RAN_name", "kpi_id", "value"]
        )
    h = pd.read_csv(p, parse_dates=["date"])
    h["date"] = pd.to_datetime(h["date"]).dt.normalize()
    return h


def trim_history(h: pd.DataFrame) -> pd.DataFrame:
    if h.empty:
        return h
    cutoff = h["date"].max() - pd.Timedelta(days=HISTORY_MAX_DAYS)
    return h[h["date"] >= cutoff].copy()


def merge_history(existing: pd.DataFrame, new_rows: pd.DataFrame) -> pd.DataFrame:
    if new_rows.empty:
        return trim_history(existing)
    combined = pd.concat([existing, new_rows], ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.normalize()
    combined = combined.sort_values("date", kind="stable").drop_duplicates(
        subset=["tech", "date", "DN", "kpi_id"], keep="last"
    )
    return trim_history(combined)


def history_to_panel(profile: dict, history: pd.DataFrame) -> pd.DataFrame:
    """Rebuild wide panel for one RAT from long history."""
    h = history[history["tech"] == profile["tech"]].copy()
    if h.empty:
        return pd.DataFrame()
    meta_cols = ["DN", "date", "sitecode", "area", "RAN_name", "cell_name"]
    meta_cols = [c for c in meta_cols if c in h.columns]
    wide = h.pivot_table(index=["date", "DN"], columns="kpi_id", values="value", aggfunc="mean")
    wide = wide.reset_index()
    meta = h.drop_duplicates("DN").set_index("DN")
    for c in meta_cols:
        if c in ("date", "DN"):
            continue
        if c in meta.columns:
            wide[c] = wide["DN"].map(meta[c])
    return wide


def fact_to_baseline(fact: pd.DataFrame) -> pd.DataFrame:
    """Small comparison snapshot (what cells are measured against)."""
    cols = [
        "tech",
        "as_of_date",
        "DN",
        "cell_name",
        "sitecode",
        "area",
        "kpi_id",
        "kpi_name",
        "direction",
        "current_value",
        "forecast_value",
        "band_low",
        "band_high",
        "z_score",
        "wow_lags_available",
        "wow_1_pct_signed",
        "trend_label",
        "severity_score",
        "rank_nw",
    ]
    cols = [c for c in cols if c in fact.columns]
    return fact[cols].copy()


def last_history_date(history: pd.DataFrame, tech: str) -> pd.Timestamp | None:
    sub = history[history["tech"] == tech]
    if sub.empty:
        return None
    return sub["date"].max()


def filter_raw_after(raw: pd.DataFrame, after: pd.Timestamp | None) -> pd.DataFrame:
    if after is None or raw.empty:
        return raw
    raw = raw.copy()
    raw["_date"] = pd.to_datetime(raw["PERIOD_START_TIME"]).dt.normalize()
    out = raw[raw["_date"] > after].drop(columns=["_date"])
    return out


def run_incremental_for_profile(
    profile: dict,
    raw: pd.DataFrame,
    history: pd.DataFrame,
    full_rebuild: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Returns (updated_history, fact, baseline).
    If full_rebuild or no history: use all raw; else only dates after last history date.
    """
    tech = profile["tech"]
    last = None if full_rebuild else last_history_date(history, tech)
    chunk = raw if full_rebuild or last is None else filter_raw_after(raw, last)
    if chunk.empty and last is not None:
        panel = history_to_panel(profile, history)
    else:
        panel_new = panel_from_raw(profile, chunk if not chunk.empty else raw)
        new_rows = panel_to_history_rows(profile, panel_new)
        history = merge_history(history, new_rows)
        panel = history_to_panel(profile, history)

    if panel.empty:
        return history, pd.DataFrame(), pd.DataFrame()

    fact, _, _ = build_facts_for_tech(profile, panel)
    baseline = fact_to_baseline(fact)
    return history, fact, baseline
