"""Cell KPI trend engine — used by CLI and Power BI Python scripts."""

from __future__ import annotations

import numpy as np
import pandas as pd

BAND_Z_WATCH = 1.5
BAND_Z_BAD = 2.5
MIN_LAGS = 2
MIN_NETWORK_FILL = 0.05
EPS = 1e-6
# Drop rates sit near 0 (typical cell ~0.05%), so relative rules flag single dropped calls.
# For KPIs with "abs_floor", sigma is at least this many percentage points and a week only
# votes degrading/improving when the absolute change also exceeds it.
DROP_ABS_FLOOR = 0.5
# Rates outside 0-100% (e.g. negative drop rates from report formulas) are treated as missing.
PERCENT_KPIS = {"VOICE_SR", "DATA_SR", "VOICE_DROP"}

TECH_PROFILES: list[dict] = [
    {
        "tech": "2G",
        "folder": "2g",
        "name_col": "BCF_name",
        "kpis": [
            {"id": "VOICE_TRAFFIC", "label": "2G speech traffic (busy hour)", "columns": ["2G_Busy_hour_Traffic__Speech"], "direction": "up", "weight": 1.0},
            {"id": "DATA_TRAFFIC", "label": "2G data traffic volume (DL+UL)", "columns": ["Data_Traffic_Vol_DL", "Data_Traffic_Vol_UL"], "sum_columns": True, "direction": "up", "weight": 1.0},
            {"id": "VOICE_SR", "label": "2G call setup success rate (speech)", "columns": ["2G_Call_Setup_Success_RateSpeech"], "direction": "up", "weight": 1.3},
            {"id": "DATA_SR", "label": "2G call setup success rate (data)", "columns": ["2G_Call_Setup_Success_Rate__Data"], "direction": "up", "weight": 1.3},
            {"id": "VOICE_DROP", "label": "2G drop call rate (speech)", "columns": ["2G_Drop_Call_RateSpeech_new"], "direction": "down", "weight": 1.4, "abs_floor": DROP_ABS_FLOOR},
        ],
    },
    {
        "tech": "3G",
        "folder": "3g",
        "name_col": "WCEL_name",
        "kpis": [
            {"id": "VOICE_TRAFFIC", "label": "3G speech traffic", "columns": ["TRAFFIC_SPEECH2"], "direction": "up", "weight": 1.0},
            {"id": "DATA_TRAFFIC", "label": "3G PS traffic (HSDPA+HSUPA+R99 DL+UL)", "columns": ["TRAFFIC_HSDPA2", "TRAFFIC_HSUPA2", "TRAFFIC_R99DLLINK2", "TRAFFIC_R99UPLINK2"], "sum_columns": True, "direction": "up", "weight": 1.0},
            {"id": "VOICE_SR", "label": "3G call setup success (speech PCH)", "columns": ["3G_Call_Setup_SuccessVoice_PCH"], "direction": "up", "weight": 1.3},
            {"id": "DATA_SR", "label": "3G call setup success rate (HSDPA)", "columns": ["3G_CALL_SETUP_SUCCESS_RATE__HSDPA"], "direction": "up", "weight": 1.3},
            {"id": "VOICE_DROP", "label": "3G voice drop call rate", "columns": ["3G_VOICE_DCR"], "direction": "down", "weight": 1.4, "abs_floor": DROP_ABS_FLOOR},
        ],
    },
    {
        "tech": "LTE",
        "folder": "4G",
        "name_col": "LNCEL_name",
        "kpis": [
            {"id": "VOICE_TRAFFIC", "label": "VoLTE total traffic", "columns": ["VoLTE_total_traffic"], "direction": "up", "weight": 1.0},
            {"id": "DATA_TRAFFIC", "label": "LTE PDCP SDU volume (DL+UL)", "columns": ["PDCP_SDU_Volume_DL", "PDCP_SDU_Volume_UL"], "sum_columns": True, "direction": "up", "weight": 1.0},
            {"id": "VOICE_SR", "label": "ERAB setup SR QCI1 (VoLTE)", "columns": ["ERAB_Stp_SR_QCI1"], "direction": "up", "weight": 1.3},
            {"id": "DATA_SR", "label": "E-UTRAN ERAB setup SR", "columns": ["EUTRAN_ERAB_stp_SR"], "direction": "up", "weight": 1.3},
            {"id": "DL_THP", "label": "Avg PDCP cell throughput DL", "columns": ["Avg_PDCP_cell_thp_DL"], "direction": "up", "weight": 1.2},
            {"id": "UL_THP", "label": "Avg PDCP cell throughput UL", "columns": ["Avg_PDCP_cell_thp_UL"], "direction": "up", "weight": 1.2},
            {"id": "VOICE_DROP", "label": "ERAB QCI1 drop ratio (RAN view)", "columns": ["ERAB_QCI1_DR_RAN_view"], "direction": "down", "weight": 1.4, "abs_floor": DROP_ABS_FLOOR},
        ],
    },
]


def profile_by_tech(tech: str) -> dict:
    for p in TECH_PROFILES:
        if p["tech"] == tech:
            return p
    raise KeyError(f"Unknown tech {tech!r}; use 2G, 3G, LTE")


def kpi_value_columns(kpi: dict) -> list[str]:
    return list(kpi["columns"])


def mad(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return float("nan")
    med = np.median(x)
    return float(np.median(np.abs(x - med)))


def signed_pct(current: float, ref: float, direction: str) -> float:
    if not np.isfinite(current) or not np.isfinite(ref):
        return float("nan")
    denom = max(abs(ref), EPS)
    raw = (current - ref) / denom * 100.0
    return raw if direction == "up" else -raw


def trend_label(z: float, lags_degrading: int, lags_improving: int, k: int) -> str:
    if not np.isfinite(z):
        return "Insufficient_data"
    if z <= -BAND_Z_BAD or (k and lags_degrading / k >= 2 / 3):
        return "Degrading"
    if z >= BAND_Z_BAD or (k and lags_improving / k >= 2 / 3):
        return "Improving"
    if abs(z) >= BAND_Z_WATCH:
        return "Watch"
    return "Stable"


def panel_from_raw(profile: dict, raw: pd.DataFrame) -> pd.DataFrame:
    """Build daily panel from PM export (Power BI `dataset` or combined CSV rows)."""
    df = raw.copy()
    if "PERIOD_START_TIME" not in df.columns:
        raise ValueError("Missing PERIOD_START_TIME column")
    if "DN" not in df.columns:
        raise ValueError("Missing DN column")

    df["date"] = pd.to_datetime(df["PERIOD_START_TIME"], errors="coerce").dt.normalize()
    df = df.drop(columns=["PERIOD_START_TIME"], errors="ignore")

    meta_cols = ["sitecode", "area", "RAN_name", profile["name_col"]]
    for kpi in profile["kpis"]:
        for c in kpi_value_columns(kpi):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in meta_cols:
            if c in df.columns:
                continue

    # Stable sort keeps file load order within a day, so the newest file wins on duplicates.
    df = df.sort_values("date", kind="stable").drop_duplicates(subset=["date", "DN"], keep="last")

    for kpi in profile["kpis"]:
        cols = [c for c in kpi_value_columns(kpi) if c in df.columns]
        if not cols:
            df[kpi["id"]] = np.nan
            continue
        if kpi.get("sum_columns"):
            df[kpi["id"]] = df[cols].sum(axis=1, min_count=1)
        else:
            df[kpi["id"]] = df[cols[0]]
        if kpi["id"] in PERCENT_KPIS:
            df[kpi["id"]] = df[kpi["id"]].where(df[kpi["id"]].between(0, 100))

    return df


def valid_dates(panel: pd.DataFrame, kpi: dict) -> set:
    """Dates with enough cells reporting the KPI. For summed traffic KPIs a 0 counts as missing,
    because an export with empty counters sums to 0 for every cell."""
    s = panel[kpi["id"]]
    reported = s.notna() & (s != 0) if kpi.get("sum_columns") else s.notna()
    fill = reported.groupby(panel["date"]).mean()
    return set(fill[fill >= MIN_NETWORK_FILL].index)


def effective_as_of(panel: pd.DataFrame, kpi: dict, valid: set | None = None) -> pd.Timestamp:
    valid = valid_dates(panel, kpi) if valid is None else valid
    return max(valid) if valid else panel["date"].max()


def add_label_ranks(fact: pd.DataFrame) -> pd.DataFrame:
    """rank_degrading / rank_improving: 1..N within (tech, kpi_id) among that label only, blank otherwise.
    Many cells tie at the z cap, so ties are broken by relative distance from the forecast."""
    rel = (fact["current_value"] - fact["forecast_value"]).abs() / fact["forecast_value"].abs().clip(lower=EPS)
    order = fact.assign(_rel=rel.fillna(0)).sort_values(["severity_score", "_rel"], ascending=False, kind="stable")
    for label, col in (("Degrading", "rank_degrading"), ("Improving", "rank_improving")):
        sub = order[order["trend_label"] == label]
        fact[col] = (sub.groupby(["tech", "kpi_id"]).cumcount() + 1).reindex(fact.index).astype("Int64")
    return fact


def build_facts_for_tech(profile: dict, panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    global_min = panel["date"].min()
    dates = set(panel["date"].unique())
    meta = panel.drop_duplicates("DN").set_index("DN")
    name_col = profile["name_col"]
    fact_rows: list[dict] = []
    lag_rows: list[dict] = []
    kpi_as_ofs: dict[str, str] = {}

    for kpi in profile["kpis"]:
        kid = kpi["id"]
        abs_floor = float(kpi.get("abs_floor", 0.0))
        kpi_dates = valid_dates(panel, kpi)
        t_max = effective_as_of(panel, kpi, kpi_dates)
        kpi_as_ofs[kid] = t_max.date().isoformat()
        dow_name = t_max.day_name()

        available_lags: list[tuple[int, pd.Timestamp]] = []
        k = 1
        while True:
            lag_date = t_max - pd.Timedelta(days=7 * k)
            if lag_date < global_min or lag_date not in kpi_dates:
                break
            available_lags.append((k, lag_date))
            k += 1

        by_date = {d: panel[panel["date"] == d].set_index("DN")[kid] for d in panel["date"].unique()}
        at_t = panel[panel["date"] == t_max].set_index("DN")

        for dn in at_t.index:
            current = at_t.at[dn, kid]
            if not np.isfinite(current):
                continue

            refs: list[float] = []
            lags_deg = lags_imp = 0
            wow1 = np.nan

            for lag_k, lag_date in available_lags:
                ref_s = by_date.get(lag_date)
                ref = ref_s.get(dn, np.nan) if ref_s is not None else np.nan
                if not np.isfinite(ref):
                    continue
                refs.append(float(ref))
                sp = signed_pct(float(current), ref, kpi["direction"])
                if lag_k == 1 and np.isfinite(sp):
                    wow1 = sp
                if np.isfinite(sp) and abs(float(current) - ref) > abs_floor:
                    if sp < -5:
                        lags_deg += 1
                    elif sp > 5:
                        lags_imp += 1
                lag_rows.append(
                    {
                        "tech": profile["tech"],
                        "as_of_date": t_max.date().isoformat(),
                        "DN": dn,
                        "kpi_id": kid,
                        "lag_weeks": lag_k,
                        "ref_date": lag_date.date().isoformat(),
                        "ref_value": round(float(ref), 6),
                        "wow_pct_signed": round(sp, 2) if np.isfinite(sp) else None,
                    }
                )

            k_avail = len(refs)
            if k_avail < MIN_LAGS:
                label = "Insufficient_data"
                forecast = band_low = band_high = z = np.nan
            else:
                forecast = float(np.median(refs))
                sigma = mad(np.array(refs))
                floor = max(abs(forecast) * 0.05, abs_floor, EPS)
                if not np.isfinite(sigma) or sigma < floor:
                    sigma = floor
                z_raw = (float(current) - forecast) / sigma
                z = z_raw if kpi["direction"] == "up" else -z_raw
                z = float(np.clip(z, -20, 20))
                band_low = forecast - BAND_Z_BAD * sigma
                band_high = forecast + BAND_Z_BAD * sigma
                label = trend_label(z, lags_deg, lags_imp, k_avail)

            severity = abs(z) * kpi["weight"] if np.isfinite(z) else 0.0
            m = meta.loc[dn] if dn in meta.index else {}

            fact_rows.append(
                {
                    "tech": profile["tech"],
                    "as_of_date": t_max.date().isoformat(),
                    "dow": dow_name,
                    "DN": dn,
                    "cell_name": m.get(name_col, ""),
                    "sitecode": m.get("sitecode", ""),
                    "area": m.get("area", ""),
                    "RAN_name": m.get("RAN_name", ""),
                    "kpi_id": kid,
                    "kpi_name": kpi["label"],
                    "direction": kpi["direction"],
                    "current_value": round(float(current), 6),
                    "forecast_value": round(forecast, 6) if np.isfinite(forecast) else None,
                    "band_low": round(band_low, 6) if np.isfinite(band_low) else None,
                    "band_high": round(band_high, 6) if np.isfinite(band_high) else None,
                    "z_score": round(float(z), 2) if np.isfinite(z) else None,
                    "wow_lags_available": k_avail,
                    "wow_1_pct_signed": round(wow1, 2) if np.isfinite(wow1) else None,
                    "lags_degrading": lags_deg,
                    "lags_improving": lags_imp,
                    "trend_label": label,
                    "in_band": int(band_low <= current <= band_high)
                    if np.isfinite(band_low) and np.isfinite(band_high)
                    else None,
                    "severity_score": round(severity, 3),
                }
            )

    fact = pd.DataFrame(fact_rows)
    if not fact.empty:
        fact["rank_nw"] = fact.groupby(["tech", "kpi_id"])["severity_score"].rank(
            method="dense", ascending=False
        ).astype("Int64")
        fact = add_label_ranks(fact)

    summary = {
        "tech": profile["tech"],
        "file_date_max": panel["date"].max().date().isoformat(),
        "file_date_min": global_min.date().isoformat(),
        "kpi_as_of_dates": kpi_as_ofs,
        "unique_days": int(panel["date"].nunique()),
        "unique_cells": int(panel["DN"].nunique()),
        "panel_rows": int(len(panel)),
    }
    return fact, pd.DataFrame(lag_rows), summary


def run_tech_from_raw(tech: str, raw: pd.DataFrame) -> pd.DataFrame:
    """Power BI entry: raw PM rows → fact trend table for one RAT."""
    profile = profile_by_tech(tech)
    panel = panel_from_raw(profile, raw)
    fact, _, _ = build_facts_for_tech(profile, panel)
    return fact
