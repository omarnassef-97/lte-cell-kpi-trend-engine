# lte-cell-kpi-trend-engine

Statistical week-over-week trend detection across 2G, 3G and LTE cell KPIs, with incremental daily state so it can run unattended.

## Problem

Network teams need to know, every day, which cells are quietly degrading across dozens of KPIs (traffic, call-setup success, drop rate, throughput) before customers notice. Doing that by eye across three technologies and thousands of cells doesn't scale, and a naive "compare to yesterday" check is noisy — a single bad day gets flagged as real degradation.

## Approach

- **Config-driven KPI catalog per technology.** Each KPI maps to one or more raw export columns, declares its "better direction" (up/down) and a severity weight, so adding a KPI or a technology is a data change, not a code change.
- **Robust to messy exports.** Builds a daily per-cell panel from raw PM exports, matching columns by header rather than position, and auto-detecting delimiter and header length so inconsistent exports from different report tools still parse.
- **Seasonality-aware comparison.** Each cell/KPI reading is compared against several prior same-weekday readings (a Monday is only measured against other Mondays), removing day-of-week noise from the baseline.
- **Robust scoring.** Deviations are scored with a median/MAD-based (median absolute deviation) Z-score rather than mean/stdev, so a single bad day doesn't distort the baseline the way it would with a standard deviation.
- **Clear verdicts.** Every cell/KPI pair is classified as Degrading, Watch, Improving, Stable, or Insufficient_data, and ranked by severity within its KPI for quick triage.
- **Incremental state.** A rolling ~90-day history file plus a small baseline snapshot means a daily scheduled run only has to process new rows, not reload months of exports every time.

## Tech stack

Python · pandas · NumPy

## How to run

```
pip install -r requirements.txt
```

Point the engine at a folder of daily PM export CSVs, with one subfolder per technology (`2g/`, `3g/`, `4G/`):

```
set CELL_KPI_BASE=path\to\your\exports
python cell_kpi_trend_multi_tech.py
```

Outputs land in `cell_kpi_trend_out/`: a fact table (`fact_cell_kpi_trend.csv`) covering every cell/KPI pair, a rollup of the worst cells per technology, and a rolling history file used by the next incremental run.

## Project layout

- `cell_kpi_trend_engine.py` — the core engine: KPI catalog, panel building, robust trend scoring. Framework-agnostic — this is the same module a Power BI Python-script data source calls directly.
- `cell_kpi_incremental.py` — rolling history and baseline state so a scheduled run only processes what's new.
- `cell_kpi_trend_multi_tech.py` — CLI entry point: loads raw exports for all three technologies, runs the engine, writes the output CSVs.

This engine backs a Power BI dashboard that ranks degrading cells by severity and routes them by area. The Power BI project-generation scripts aren't included here, since they embed the specific SharePoint site layout of the environment they publish to.

## Note on data

No real network data is included. `CELL_KPI_BASE` defaults to a placeholder `data/` folder — point it at your own exports.
