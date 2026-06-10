#!/usr/bin/env python3
# Copyright 2026 OpenHW Group
# SPDX-License-Identifier: Apache-2.0
"""Generate CVA6 PD dashboard HTML from collected JSON data.

Reads per-workflow JSON files and renders a Jinja2 template into
a self-contained static HTML file.
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from typing import TypedDict

from jinja2 import Environment, FileSystemLoader

# Workflow display info (order matters for UI)
PD_WORKFLOW = [
    {"key": "pd", "display_name": "OR-design-flow.yml", "file": "runs_PD.json"}
]

TREND_COUNT = 20


def format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds <= 0:
        return "N/A"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes >= 60:
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"
    return f"{minutes}m {secs}s"


def format_datetime(iso_str: str) -> str:
    """Format ISO datetime to readable string."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return iso_str


def load_workflow_data(data_dir: Path) -> dict:
    """Load all workflow JSON data files.
    
    Raises:
        FileNotFoundError: If the data directory doesn't exist or required files are missing.
        json.JSONDecodeError: If a JSON file is malformed.
    """
    result = {}
    
    # Check if data directory exists
    if not data_dir.exists():
        raise FileNotFoundError(
            f"Data directory not found: {data_dir.resolve()}\n"
            f"  Create it or use --data-dir to specify the correct path."
        )
    
    for wf in PD_WORKFLOW:
        path = data_dir / wf["file"]
        
        if not path.exists():
            raise FileNotFoundError(
                f"Workflow data file not found: {path.resolve()}\n"
                f"  Expected file: {wf['file']}\n"
                f"  In directory: {data_dir.resolve()}\n"
                f"  Check the file path and permissions."
            )
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f, parse_float=lambda x : round(float(x),3))
                if not isinstance(data, list):
                    raise ValueError(
                        f"Expected JSON array (list of runs) in {path.name}, "
                        f"but got {type(data).__name__}"
                    )
                result[wf["key"]] = data
                print(f"Loaded {len(data)} runs from {path.name}")
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in {path.resolve()}: {e.msg} at line {e.lineno}, col {e.colno}",
                e.doc,
                e.pos
            )
        except (IOError, OSError) as e:
            raise IOError(
                f"Cannot read {path.resolve()}: {e.strerror}\n"
                f"  Check file permissions and ensure the file is readable."
            )
    
    return result


class FlowMetrics(TypedDict):
    """Per-flow metrics time series for charting.
    Each list contains values aligned across TREND_COUNT runs,
    indexed chronologically (oldest to newest).
    """
    fmax_mhz: list
    stdcell_kgate: list
    worst_setup_slack_ps: list
    timing_met: list

class ChartData(TypedDict):
    """Aggregated trend data for a single workflow.
    Ready for Chart.js consumption: labels and series are parallel arrays.
    """
    labels: list[str]
    series: dict[str, FlowMetrics]
    pass_rates: list[float]
    durations: list[float]

def build_chart_data(all_data: dict) -> dict[str, ChartData]:
    """Build Chart.js data for trend charts."""
    chart_data: dict[str, ChartData] = {}
    
    for wf in PD_WORKFLOW:
        key = wf["key"]
        runs = all_data.get(key, [])

        # Take last TREND_COUNT runs, reversed for chronological order
        trend_runs = list(reversed(runs[:TREND_COUNT]))

        labels = []
        pass_rates = []
        durations = []

        # Use defaultdict to avoid pre-initializing all possible flow keys.
        series: dict[str, FlowMetrics] = defaultdict(lambda: {
            "fmax_mhz": [],
            "stdcell_kgate": [],
            "worst_setup_slack_ps": [],
            "timing_met": []
        })

        # Collect all unique flow keys (first pass)
        all_flow_keys = set()
        for run in trend_runs:
            for flow in run.get("flows", []):
                flow_key = f"{flow['arch']}_{flow['config']}"
                all_flow_keys.add(flow_key)

        # Process each run (second pass)
        for run in trend_runs:
            # Accumulate run-level metrics (pass rate, duration)
            labels.append(str(run.get("run_number", "")))
            total = run.get("total_flows", 0)
            passed = run.get("passed_flows", 0)
            rate = round(passed / total * 100, 1) if total > 0 else 0
            pass_rates.append(rate)
            dur_min = round(run.get("duration_seconds", 0) / 60, 1)
            durations.append(dur_min)

            # Build O(1) lookup dict for flows in this run
            flow_dict = {}
            for flow in run.get("flows", []):
                flow_key = f"{flow['arch']}_{flow['config']}"
                flow_dict[flow_key] = flow.get("metrics", {})

            # For each flow_key, append metric or None
            for flow_key in all_flow_keys:
                if flow_key in flow_dict:
                    metrics = flow_dict[flow_key]
                    series[flow_key]["fmax_mhz"].append(metrics.get("fmax_mhz", 0))
                    series[flow_key]["stdcell_kgate"].append(metrics.get("stdcell_kgate", 0))
                    series[flow_key]["worst_setup_slack_ps"].append(metrics.get("worst_setup_slack_ps", 0))
                    series[flow_key]["timing_met"].append(metrics.get("timing_met", False))
                else:
                    # Flow absent from this run: fill with None
                    series[flow_key]["fmax_mhz"].append(None)
                    series[flow_key]["stdcell_kgate"].append(None)
                    series[flow_key]["worst_setup_slack_ps"].append(None)
                    series[flow_key]["timing_met"].append(None)


        chart_data[key] = {
            "labels": labels,
            "series": dict(series),
            "pass_rates": pass_rates,
            "durations": durations,
        }

    return chart_data


def enrich_run(run: dict) -> dict:
    """Add display-friendly fields to a run dict."""
    run["duration_display"] = format_duration(run.get("duration_seconds", 0))
    run["created_at_display"] = format_datetime(run.get("created_at", ""))
    for flow in run.get("flows", []):
        flow["duration_display"] = format_duration(flow.get("duration_seconds", 0))
    return run


def build_workflows_context(all_data: dict) -> list:
    """Build the workflows list for the template context."""
    workflows = []
    for wf in PD_WORKFLOW:
        key = wf["key"]
        runs = all_data.get(key, [])

        # Enrich all runs
        for run in runs:
            enrich_run(run)

        # Build latest run summary (or placeholder)
        if runs:
            latest = runs[0]
        else:
            latest = {
                "conclusion": "unknown",
                "head_branch": "N/A",
                "head_sha": "N/A",
                "passed_flows": 0,
                "failed_flows": 0,
                "skipped_flows": 0,
                "total_flows": 0,
                "duration_display": "N/A",
                "run_number": 0,
                "html_url": "#",
                "created_at_display": "N/A"
            }

        workflows.append(
            {
                "key": key,
                "display_name": wf["display_name"],
                "latest": latest,
                "runs": runs,
            }
        )

    return workflows


def main():
    parser = argparse.ArgumentParser(
        description="Generate CVA6 CI Dashboard HTML"
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing JSON data files",
    )
    parser.add_argument(
        "--output-dir",
        default="site",
        help="Output directory for generated HTML",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "openhwgroup/cva6"),
        help="GitHub repository (owner/name)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    try:
        all_data = load_workflow_data(data_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=__import__('sys').stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"JSON ERROR: {e}", file=__import__('sys').stderr)
        return 1
    except IOError as e:
        print(f"READ ERROR: {e}", file=__import__('sys').stderr)
        return 1
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}", file=__import__('sys').stderr)
        return 1

    # Build template context
    now = datetime.now(timezone.utc)
    workflows = build_workflows_context(all_data)
    chart_data = build_chart_data(all_data)

    default_matrix_wf = "pd"
    if not all_data.get("pd"):
        for wf in PD_WORKFLOW:
            if all_data.get(wf["key"]):
                default_matrix_wf = wf["key"]
                break

    context = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "year": now.year,
        "repo": args.repo,
        "workflows": workflows,
        "default_matrix_wf": default_matrix_wf,
        "chart_data_json": json.dumps(chart_data),
        "trend_count": TREND_COUNT
    }

    # Render template
    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("PD_dashboard.html")
    html = template.render(**context)

    # Write output
    output_file = output_dir / "PD_dashboard.html"
    with open(output_file, "w") as f:
        f.write(html)

    print(f"Dashboard generated: {output_file}")
    print(f"  Workflows: {len(workflows)}")
    for wf in workflows:
        print(f"    - {wf['display_name']}: {len(wf['runs'])} runs")
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
