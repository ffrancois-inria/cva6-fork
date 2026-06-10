#!/usr/bin/env python3
# Copyright 2026 OpenHW Group
# SPDX-License-Identifier: Apache-2.0
"""Collect OpenROAD design flow PPA metrics for the CVA6 dashboard.

Fetches completed runs of the OpenRoad design flow workflow from the GitHub API,
downloads the GRT JSON artifacts, and extracts PPA metrics.
Requires `gh` CLI authenticated (pre-installed on GHA runners).

Intended for use in PD-dashboard.yml, triggered via
workflow_run on OR-design-flow.yml:

    python3 extract_PD_metrics.py \\
        --repo openhwgroup/cva6 \\
        --data-dir /tmp/synth-data \\
        --fetch-count 20

Artifact layout expected from OR-design-flow.yml:

    Artifact name:  PD-grt-{arch}-{config}
    Contents:       5_1_grt.json

The script downloads all PD-grt-* artifacts for each run using
`gh run download` (which handles authentication and zip extraction),
then processes the extracted JSON files.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Synthesis workflow file to track.
PD_WORKFLOW = "OR-design-flow.yml"

# GRT JSON produced at the end of the global-route stage.
GRT_JSON_FILENAME = "5_1_grt.json"

# Artifact name prefix uploaded by OR-design-flow.yml.
PD_ARTIFACT_PREFIX = "PD-grt-"

# Reference cell area for Kgate calculation (µm²).
# A "gate equivalent" = area of a 2-input NAND cell.
# ASAP7 RVT NAND2x1: 0.0874 µm².
# Override with --nand2-area for other PDKs.
ASAP7_NAND2_AREA_UM2 = 0.0874

MAX_HISTORY = 50


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------

def extract_metrics(data: dict, nand2_area: float) -> dict:
    """Extract PPA metrics from a GRT-stage JSON report."""
    def get(key):
        v = data.get(key)
        if v is None:
            raise KeyError(f"Key not found in JSON: {key}")
        return v

    stdcell_area_um2 = get("globalroute__design__instance__area__stdcell")
    macro_area_um2   = get("globalroute__design__instance__area__macros")
    total_area_um2   = get("globalroute__design__instance__area")
    die_area_um2     = get("globalroute__design__die__area")
    core_area_um2    = get("globalroute__design__core__area")
    fmax_hz          = get("globalroute__timing__fmax")
    worst_slack_ps   = get("globalroute__timing__setup__ws")

    return {
        "stdcell_area_um2":    round(stdcell_area_um2, 4),
        "macro_area_um2":      round(macro_area_um2, 4),
        "total_instance_area_um2": round(total_area_um2, 4),
        "core_area_um2":       round(core_area_um2, 4),
        "die_area_um2":        round(die_area_um2, 4),
        "stdcell_kgate":       round(stdcell_area_um2 / nand2_area / 1000, 4),
        "fmax_mhz":            round(fmax_hz / 1e6, 2),
        "worst_setup_slack_ps": round(worst_slack_ps, 3),
        "timing_met":          worst_slack_ps >= 0,
    }


# ---------------------------------------------------------------------------
# JSON persistence helpers  (mirrors .github/scripts/dashboard_tiers/collect_data.py)
# ---------------------------------------------------------------------------

def load_existing(path: Path) -> list:
    """Load existing run data from a JSON file."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            print(f"WARNING: Could not read {path}, starting fresh", file=sys.stderr)
    return []


def merge_runs(existing: list, new_runs: list) -> list:
    """Merge new runs into existing data, deduplicating by run_id."""
    existing_ids = {r["id"] for r in existing}
    merged = list(existing)

    for run in new_runs:
        if run["id"] not in existing_ids:
            merged.append(run)
            existing_ids.add(run["id"])

    # Sort by created_at descending (newest first)
    merged.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    # Trim to MAX_HISTORY
    return merged[:MAX_HISTORY]


def duration_seconds(started_at: str, completed_at: str) -> int:
    """Calculate duration in seconds between two ISO timestamps."""
    if not started_at or not completed_at:
        return 0
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end   = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        return max(0, int((end - start).total_seconds()))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# GitHub API helpers  (mirrors collect_data.py)
# ---------------------------------------------------------------------------

def gh_api(endpoint: str, repo: str) -> dict:
    """Call GitHub API via `gh api` and return parsed JSON."""
    url    = f"/repos/{repo}/actions/{endpoint}"
    result = subprocess.run(
        ["gh", "api", url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: gh api {url} failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def fetch_runs(repo: str, workflow_file: str, count: int) -> list:
    """Fetch the latest `count` completed workflow runs."""
    data = gh_api(
        f"workflows/{workflow_file}/runs?status=completed&per_page={count}",
        repo,
    )
    return data.get("workflow_runs", [])[:count]


# ---------------------------------------------------------------------------
# Artifact download
# ---------------------------------------------------------------------------

def download_run_artifacts(repo: str, run_id: int, dest_dir: Path) -> list:
    """Download all PD-grt-* artifacts for a run into dest_dir.

    Uses `gh run download` which handles authentication and zip extraction.
    Each artifact lands at dest_dir/{artifact_name}/5_1_grt.json.

    Artifact naming convention (set by OR-design-flow.yml):
        PD-grt-{arch}-{config}

    Returns a list of (arch, config, grt_json_path) tuples.
    """
    result = subprocess.run(
        [
            "gh", "run", "download", str(run_id),
            "--repo", repo,
            "--pattern", f"{PD_ARTIFACT_PREFIX}*",
            "--dir", str(dest_dir),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  WARNING: gh run download failed for run {run_id}: {result.stderr}",
            file=sys.stderr,
        )
        return []

    found = []
    for artifact_dir in sorted(dest_dir.iterdir()):
        if not artifact_dir.is_dir():
            continue
        artifact_name = artifact_dir.name  # e.g. "PD-grt-L1MetadataArray-base"
        if not artifact_name.startswith(PD_ARTIFACT_PREFIX):
            continue

        # Parse arch and config from artifact name.
        # rsplit on "-" (last occurrence) so design names containing "-" are preserved.
        suffix  = artifact_name[len(PD_ARTIFACT_PREFIX):]  # "L1MetadataArray-base"
        parts   = suffix.rsplit("-", 1)
        arch  = parts[0] if len(parts) >= 1 else suffix
        config = parts[1] if len(parts) >= 2 else "base"

        grt_path = artifact_dir / GRT_JSON_FILENAME
        if grt_path.exists():
            found.append((arch, config, grt_path))
            print(f"  Downloaded: {artifact_name} -> {arch}/{config}")
        else:
            print(
                f"  WARNING: {GRT_JSON_FILENAME} not found in artifact {artifact_name}",
                file=sys.stderr,
            )

    return found


# ---------------------------------------------------------------------------
# CI run processing
# ---------------------------------------------------------------------------

def process_ci_run(repo: str, run: dict, nand2_area: float) -> dict:
    """Fetch artifacts for a PD run, extract metrics, and build a run record."""
    run_id = run["id"]

    with tempfile.TemporaryDirectory() as tmp:
        grt_list = download_run_artifacts(repo, run_id, Path(tmp))

        flows = []
        for arch, config, grt_path in grt_list:
            try:
                with open(grt_path) as f:
                    data = json.load(f)
                metrics    = extract_metrics(data, nand2_area)
                conclusion = "success"
                timing_str = "timing met" if metrics["timing_met"] else "timing NOT met"
                print(
                    f"    [SUCCESS] {arch}/{config}  "
                    f"fmax={metrics['fmax_mhz']:.1f} MHz  "
                    f"stdcell={metrics['stdcell_area_um2']:.3f} µm²  "
                    f"({metrics['stdcell_kgate']:.2f} Kgate)  [{timing_str}]"
                )
            except (json.JSONDecodeError, KeyError, IOError) as exc:
                print(f"    [FAILURE] {arch}/{config}: {exc}", file=sys.stderr)
                metrics    = {}
                conclusion = "failure"

            flows.append({
                "arch":             arch,
                "config":           config,
                "conclusion":       conclusion,
                "html_url":         run.get("html_url", ""),
                "duration_seconds": 0,  # per-flow timing not available from artifacts
                "metrics":          metrics,
            })

    passed_flows = sum(1 for j in flows if j["conclusion"] == "success")
    run_dur     = duration_seconds(
        run.get("run_started_at", run.get("created_at", "")),
        run.get("updated_at", ""),
    )

    return {
        "id":               run_id,
        "run_number":       run.get("run_number", 0),
        "conclusion":       run.get("conclusion", "unknown"),
        "html_url":         run.get("html_url", ""),
        "head_branch":      run.get("head_branch", ""),
        "head_sha":         run.get("head_sha", "")[:8],
        "created_at":       run.get("created_at", ""),
        "duration_seconds": run_dur,
        "total_flows":       len(flows),
        "passed_flows":      passed_flows,
        "failed_flows":      len(flows) - passed_flows,
        "flows":             flows,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect CVA6 physical design PPA metrics for the CI dashboard",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "openhwgroup/cva6"),
        help="GitHub repository (owner/name)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory to store JSON data files (default: data/)",
    )
    parser.add_argument(
        "--fetch-count",
        type=int,
        default=10,
        help="Number of recent physical design runs to fetch (default: 10)",
    )
    parser.add_argument(
        "--workflow",
        default=PD_WORKFLOW,
        help=f"Synthesis workflow filename to track (default: {PD_WORKFLOW})",
    )
    parser.add_argument(
        "--nand2-area",
        type=float,
        default=ASAP7_NAND2_AREA_UM2,
        metavar="UM2",
        help=(
            f"NAND2 cell area in µm² for Kgate calculation "
            f"(default: {ASAP7_NAND2_AREA_UM2} for ASAP7 RVT)"
        ),
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    json_path = data_dir / "runs_PD.json"
    existing  = load_existing(json_path)
    print(f"Existing records: {len(existing)}")

    print(f"\nFetching runs for {args.workflow} from {args.repo}")
    existing_ids = {r["id"] for r in existing}
    runs_raw     = fetch_runs(args.repo, args.workflow, args.fetch_count)
    print(f"Fetched {len(runs_raw)} runs from API")

    new_runs = []
    for run in runs_raw:
        if run["id"] in existing_ids:
            print(f"  Skipping run #{run['run_number']} (id={run['id']}) - already exists")
            continue

        print(f"  Processing run #{run['run_number']} (id={run['id']})...")
        processed = process_ci_run(args.repo, run, args.nand2_area)
        new_runs.append(processed)
        print(
            f"    -> {processed['total_flows']} flows: "
            f"{processed['passed_flows']} passed, "
            f"{processed['failed_flows']} failed"
        )

    merged = merge_runs(existing, new_runs)
    with open(json_path, "w") as f:
        json.dump(merged, f, indent=2)

    print(f"\nSaved {len(merged)} records to {json_path}")


if __name__ == "__main__":
    main()
