"""Multi-RAT daily cell KPI trend — batch CLI (local folders or SharePoint export copies)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from cell_kpi_trend_engine import (
    TECH_PROFILES,
    build_facts_for_tech,
    kpi_value_columns,
    panel_from_raw,
)

BASE = Path(os.environ.get("CELL_KPI_BASE", "data"))
OUT_DIR = Path(os.environ.get("CELL_KPI_OUT_DIR", str(BASE / "cell_kpi_trend_out")))
# Optional: synced SharePoint library folder (OneDrive / server sync) for Power BI web refresh
SHAREPOINT_PUBLISH_DIR = os.environ.get("CELL_KPI_SHAREPOINT_PUBLISH", "").strip()


def profiles_for_run() -> list[dict]:
    """Optional: CELL_KPI_TECHS=LTE or LTE,3G to shorten a run."""
    spec = os.environ.get("CELL_KPI_TECHS", "").strip()
    if not spec:
        return TECH_PROFILES
    allow = {t.strip().upper() for t in spec.split(",") if t.strip()}
    picked = [p for p in TECH_PROFILES if p["tech"] in allow]
    if not picked:
        raise ValueError(f"CELL_KPI_TECHS={spec!r} — use 2G, 3G, LTE (comma-separated)")
    return picked


ALIASES_FILE = BASE / "column_aliases.csv"


def load_aliases(tech: str) -> dict[str, list[str]]:
    """Export column name -> engine column name(s), from column_aliases.csv (tech,export_name,engine_name)."""
    if not ALIASES_FILE.is_file():
        return {}
    a = pd.read_csv(ALIASES_FILE, dtype=str).fillna("")
    a = a[(a["tech"].str.strip().str.upper() == tech.upper()) & (a["export_name"].str.strip() != "")]
    out: dict[str, list[str]] = {}
    for src, dst in zip(a["export_name"].str.strip(), a["engine_name"].str.strip()):
        out.setdefault(src, []).append(dst)
    return out


def load_tech_raw(profile: dict) -> tuple[pd.DataFrame, list[str]]:
    """Load all daily CSV columns needed for this RAT (full read)."""
    folder = BASE / profile["folder"]
    # Oldest file first so a re-exported day in a newer file overrides the old values.
    paths = sorted(folder.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not paths:
        raise FileNotFoundError(f"No .csv files under {folder}")

    meta_cols = ["sitecode", "area", "RAN_name", profile["name_col"]]
    need: set[str] = {"PERIOD_START_TIME", "DN", *meta_cols}
    for k in profile["kpis"]:
        need.update(kpi_value_columns(k))
    need.add(SITE_NAME_COL)
    aliases = load_aliases(profile["tech"])

    parts: list[pd.DataFrame] = []
    for path in paths:
        sep = detect_sep(path)
        header, n_header_lines = read_header(path, sep)
        mapped = {c: aliases[c] for c in header if c in aliases and c not in need}
        usecols = [c for c in header if c in need or c in mapped]
        found = (set(usecols) - set(mapped)) | {d for ds in mapped.values() for d in ds}
        if "PERIOD_START_TIME" not in found or "DN" not in found:
            raise ValueError(f"{path.name} missing PERIOD_START_TIME or DN (add them to {ALIASES_FILE.name})")
        missing = sorted(set().union(*(kpi_value_columns(k) for k in profile["kpis"])) - found)
        if missing:
            print(f"  {path.name}: KPI columns not found -> {missing}", flush=True)

        reader = pd.read_csv(
            path, sep=sep, header=None, names=header, skiprows=n_header_lines,
            usecols=usecols, chunksize=100_000, low_memory=False,
        )
        for chunk in reader:
            for src, dsts in mapped.items():
                for d in dsts:
                    chunk[d] = chunk[src]
            chunk = chunk.drop(columns=list(mapped))
            chunk["PERIOD_START_TIME"] = parse_period(chunk["PERIOD_START_TIME"])
            chunk = chunk.dropna(subset=["PERIOD_START_TIME", "DN"])
            if SITE_NAME_COL in chunk.columns:
                if "sitecode" not in chunk.columns:
                    chunk["sitecode"] = chunk[SITE_NAME_COL].astype(str).str.extract(SITECODE_RE)[0]
                chunk = chunk.drop(columns=[SITE_NAME_COL])
            parts.append(chunk)

    return pd.concat(parts, ignore_index=True), [p.name for p in paths]


SITE_NAME_COL = "site_name_for_sitecode"
SITECODE_RE = r"_([0-9A-Z]{4}DE)\s*$"


def detect_sep(path: Path) -> str:
    with open(path, encoding="utf-8", errors="ignore") as f:
        first = f.readline()
    return ";" if first.count(";") > first.count(",") else ","


def read_header(path: Path, sep: str) -> tuple[list[str], int]:
    """Column names and number of header lines. Report exports can contain column names with raw
    line breaks, which spread the header over several lines; join lines until the field count
    matches the first data row."""
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = [f.readline() for _ in range(50)]
    data_i = next((i for i, l in enumerate(lines) if re.match(r"^\s*\d{2}[./-]\d{2}[./-]\d{4}|^\s*\d{4}-\d{2}-\d{2}", l)), 1)
    joined = " ".join(l.rstrip("\r\n") for l in lines[:data_i])
    cols = [re.sub(r"\s+", " ", c).strip().strip('"') for c in joined.split(sep)]
    seen: dict[str, int] = {}
    out = []
    for c in cols:
        n = seen.get(c, 0)
        out.append(c if n == 0 else f"{c}.{n}")
        seen[c] = n + 1
    return out, data_i


def parse_period(s: pd.Series) -> pd.Series:
    """Per-file date parse. Report-style exports write MM.DD.YYYY; other exports use ISO dates.
    Parsing each file separately stops one format from blanking the other after concat."""
    s = s.astype(str).str.strip()
    if s.str.match(r"^\d{2}\.\d{2}\.\d{4}").mean() > 0.5:
        return pd.to_datetime(s.str[:10], format="%m.%d.%Y", errors="coerce")
    return pd.to_datetime(s, errors="coerce")


def load_tech_panel(profile: dict) -> tuple[pd.DataFrame, list[str]]:
    raw, sources = load_tech_raw(profile)
    return panel_from_raw(profile, raw), sources


def main() -> None:
    incremental = os.environ.get("CELL_KPI_INCREMENTAL", "1").strip().lower() in ("1", "true", "yes")
    full_rebuild = False

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_facts: list[pd.DataFrame] = []
    all_baselines: list[pd.DataFrame] = []
    run_meta: dict = {"tech_runs": [], "kpi_catalog": [], "mode": "incremental" if incremental else "full"}

    if incremental:
        from cell_kpi_incremental import (
            baseline_path,
            history_path,
            load_history,
            run_incremental_for_profile,
        )

        if os.environ.get("CELL_KPI_FULL_REBUILD", "").strip().lower() in ("1", "true", "yes"):
            full_rebuild = True
        if full_rebuild:
            print("Incremental: full history build (CELL_KPI_FULL_REBUILD=1)", flush=True)
            history = load_history(OUT_DIR, empty=True)
        else:
            history = load_history(OUT_DIR)
        if not full_rebuild and history.empty:
            print("Incremental: first run — building history from all CSV dates")
            full_rebuild = True

        for profile in profiles_for_run():
            print(f"Incremental {profile['tech']} …", flush=True)
            raw, sources = load_tech_raw(profile)
            history, fact, baseline = run_incremental_for_profile(
                profile, raw, history, full_rebuild=full_rebuild
            )
            run_meta["tech_runs"].append(
                {
                    "source_files": sources,
                    "tech": profile["tech"],
                    "history_rows": int(len(history[history["tech"] == profile["tech"]])),
                }
            )
            if not fact.empty:
                all_facts.append(fact)
            if not baseline.empty:
                all_baselines.append(baseline)
            for k in profile["kpis"]:
                run_meta["kpi_catalog"].append(
                    {"tech": profile["tech"], "kpi_id": k["id"], "label": k["label"]}
                )

        history.to_csv(history_path(OUT_DIR), index=False)
        print(f"Saved history: {history_path(OUT_DIR)} ({len(history)} rows)")
        lags = pd.DataFrame()
    else:
        all_lags: list[pd.DataFrame] = []
        for profile in profiles_for_run():
            print(f"Loading {profile['tech']} …", flush=True)
            panel, sources = load_tech_panel(profile)
            fact, lags_part, summary = build_facts_for_tech(profile, panel)
            run_meta["tech_runs"].append({"source_files": sources, **summary})
            if not fact.empty:
                all_facts.append(fact)
                all_lags.append(lags_part)
            for k in profile["kpis"]:
                run_meta["kpi_catalog"].append(
                    {"tech": profile["tech"], "kpi_id": k["id"], "label": k["label"]}
                )
        lags = pd.concat(all_lags, ignore_index=True) if all_lags else pd.DataFrame()
        all_baselines = []

    if not all_facts:
        print("No fact rows produced.")
        return

    fact = pd.concat(all_facts, ignore_index=True)

    def _save(df: pd.DataFrame, name: str) -> None:
        path = OUT_DIR / name
        try:
            df.to_csv(path, index=False)
        except PermissionError:
            df.to_csv(OUT_DIR / name.replace(".csv", "_latest.csv"), index=False)
            print(f"Locked {name} — wrote *_latest.csv")

    _save(fact, "fact_cell_kpi_trend.csv")
    if not lags.empty:
        _save(lags, "fact_cell_kpi_wow_lags.csv")

    if all_baselines:
        baseline = pd.concat(all_baselines, ignore_index=True)
        _save(baseline, "cell_kpi_baseline.csv")

    top = (
        fact[fact["trend_label"].isin(["Degrading", "Watch", "Improving"])]
        .sort_values(["tech", "kpi_id", "severity_score"], ascending=[True, True, False])
        .groupby(["tech", "kpi_id"], group_keys=False)
        .head(15)
    )
    _save(top, "top_cells_by_kpi.csv")

    rollup = (
        fact.groupby(["tech", "DN", "cell_name", "sitecode", "area"], as_index=False)
        .agg(
            worst_z=("z_score", lambda s: s.min(skipna=True)),
            max_severity=("severity_score", "max"),
            degrading_kpis=("trend_label", lambda s: (s == "Degrading").sum()),
            watch_kpis=("trend_label", lambda s: (s == "Watch").sum()),
        )
        .sort_values(["tech", "max_severity"], ascending=[True, False])
        .groupby("tech", group_keys=False)
        .head(50)
    )
    _save(rollup, "top_cells_rollup.csv")

    counts = fact.groupby(["tech", "kpi_id", "trend_label"]).size().unstack(fill_value=0)
    _save(counts.reset_index(), "trend_label_counts.csv")
    _save(pd.DataFrame(run_meta["kpi_catalog"]), "kpi_catalog.csv")

    with open(OUT_DIR / "run_summary.json", "w", encoding="utf-8") as f:
        json.dump(run_meta, f, indent=2)

    publish_to_sharepoint_sync()

    print(json.dumps(run_meta["tech_runs"], indent=2))
    print("Wrote:", OUT_DIR)


def publish_to_sharepoint_sync() -> None:
    """Copy outputs to a SharePoint-synced folder for Power BI Service (M-only refresh)."""
    if not SHAREPOINT_PUBLISH_DIR:
        return
    dest = Path(SHAREPOINT_PUBLISH_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        "fact_cell_kpi_trend.csv",
        "cell_kpi_baseline.csv",
        "fact_cell_kpi_wow_lags.csv",
        "kpi_catalog.csv",
        "run_summary.json",
    ):
        src = OUT_DIR / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    stamp = dest / "last_success.txt"
    stamp.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    print("Published to SharePoint sync folder:", dest)


if __name__ == "__main__":
    main()
