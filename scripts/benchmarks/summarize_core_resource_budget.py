"""Condense owned load-test receipts and optionally plot them with Matplotlib."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

from scripts.benchmarks.core_cpu_workers import summary


def condense(path):
    data = json.loads(path.read_text())
    samples = [row for row in data["samples"] if row["phase"] == "load"]
    steady = [row for row in samples if 10 <= row["at_s"] <= data["load_wall_s"] - 5]
    if len(steady) < 2:
        raise ValueError("Need at least two steady-load samples")
    first, last = steady[0], steady[-1]
    elapsed = last["at_s"] - first["at_s"]
    reads = [row for row in data["raw_read_latencies"] if row["phase"] == "load"]
    bins = defaultdict(list)
    for row in reads:
        bins[int(row["at_s"])].append(row["ms"])
    waits = [
        row["wait_s"] * 1000
        for row in data["shutdown"]["dispatch_trace"]
        if row["owner"].startswith("chat-")
    ]
    series = []
    for index, row in enumerate(samples):
        earlier = samples[max(0, index - 10)]
        interval = row["at_s"] - earlier["at_s"]
        series.append(
            {
                "at_s": round(row["at_s"], 3),
                "compute_cpus_10s": round(
                    (
                        row["cgroup"]["cpu"]["usage_usec"]
                        - earlier["cgroup"]["cpu"]["usage_usec"]
                    )
                    / 1e6
                    / interval,
                    4,
                )
                if interval
                else None,
                "compute_mib": round(row["cgroup"]["memory_current"] / 2**20, 2),
                "core_mib": round(row["core_rss"] / 2**20, 2),
                "pids": row["cgroup"]["pids_current"],
                "completed_tools": row["counts"]["tool_completed"],
                "active_chats": row["counts"]["active_connections"],
                "admitted": row["budget"]["admitted"] if row["budget"] else None,
                "api_p95_ms": summary(bins[int(row["at_s"])])["p95"],
                "api_reads_in_second": len(bins[int(row["at_s"])]),
            }
        )
    host = data["host_load_reads"]
    return {
        "raw_receipt": str(path),
        "policy": data["policy"],
        "chats": data["chats"],
        "calls_per_chat": data["calls_per_chat"],
        "cpu_seconds_per_call": data["cpu_seconds_per_call"],
        "historical_calls_per_chat": data["historical_calls_per_chat"],
        "completed_chats": data["completed_chats"],
        "completed_cpu_jobs": data["chats"] * data["calls_per_chat"],
        "completed_pdfs": len(data["completed_pdfs"]),
        "load_wall_s": round(data["load_wall_s"], 3),
        "steady_window_s": round(elapsed, 3),
        "steady_compute_cpus": round(
            (last["cgroup"]["cpu"]["usage_usec"] - first["cgroup"]["cpu"]["usage_usec"])
            / 1e6
            / elapsed,
            4,
        ),
        "steady_core_cpus": round(
            (last["core_cpu_s"] - first["core_cpu_s"]) / elapsed, 4
        ),
        "ordinary_load_reads_ms": summary([row["ms"] for row in reads]),
        "all_http_failures": data["http_failures"],
        "report_poll_transport_failures": data.get("poll_transport_failures", []),
        "max_compute_mib_during_load": round(
            max(row["cgroup"]["memory_current"] for row in samples) / 2**20, 2
        ),
        "max_core_mib_during_load": round(
            max(row["core_rss"] for row in samples) / 2**20, 2
        ),
        "max_compute_pids_during_load": max(
            row["cgroup"]["pids_current"] for row in samples
        ),
        "max_active_compute_jobs_during_load": max(
            row["cgroup"]["active"] for row in samples
        ),
        "max_admitted_during_load": max(row["budget"]["admitted"] for row in samples)
        if data["final_core"]["budget"]
        else None,
        "core_loop_lag_ms": samples[-1]["loop_lag_ms"],
        "whole_run_loop_lag_ms": data["shutdown"]["loop_lag_ms"],
        "kernel_cpu_max": data["initial_compute"]["cpu_max"],
        "kernel_memory_max": data["initial_compute"]["memory_max"],
        "kernel_pids_max": data["initial_compute"]["pids_max"],
        "final_budget": data["final_core"]["budget"],
        "chat_dispatch_wait_ms": summary(waits),
        "host_load_read_count": len(host),
        "host_busy_sample_count": sum(row["busy"] for row in host),
        "oldest_host_sample_s": max(row["sample_age_seconds"] for row in host),
        "recovery_turn_s": round(data["recovery_turn_s"], 3),
        "faults": data["faults"],
        "all_chats_running_before_release": data["all_chats_running_before_release"],
        "exact_tool_ledgers_and_replay": data["exact_tool_ledgers_and_replay"],
        "core_exit_code": data["core_exit_code"],
        "owned_container_removed": data["owned_container_removed"],
        "synthetic_data_removed": data["synthetic_data_removed"],
        "source_sha256": data["source_sha256"],
        "time_series": series,
    }


def plot(results, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        3, 1, figsize=(11, 10), sharex=True, constrained_layout=True
    )
    colors = ["#167b65", "#3774b4", "#bc503a"]
    for index, data in enumerate(results):
        rows = data["time_series"]
        label = f"{data['policy']}, {data['chats']} chats"
        color = colors[index % len(colors)]
        axes[0].plot(
            [r["at_s"] for r in rows],
            [r["compute_cpus_10s"] for r in rows],
            label=label,
            color=color,
        )
        latency = [r for r in rows if r["api_p95_ms"] is not None]
        axes[1].plot(
            [r["at_s"] for r in latency],
            [r["api_p95_ms"] for r in latency],
            color=color,
            alpha=0.8,
        )
        axes[2].plot(
            [r["at_s"] for r in rows], [r["compute_mib"] for r in rows], color=color
        )
    axes[0].axhline(
        4, color="#666666", linestyle="--", linewidth=1, label="environment CPU quota"
    )
    axes[0].axhline(2, color="#888888", linestyle=":", linewidth=1)
    axes[0].set_ylabel("Background CPU-equivalents\n10-second window")
    axes[0].set_ylim(0, 4.5)
    axes[0].legend(loc="upper right", fontsize=9)
    axes[1].set_ylabel("Ordinary API read p95 (ms)\nper one-second bucket")
    axes[1].set_yscale("log")
    axes[2].set_ylabel("Compute-container memory (MiB)")
    axes[2].set_xlabel("Seconds since synthetic work was released")
    for axis in axes:
        axis.grid(alpha=0.2)
    fig.suptitle(
        "One Core under heavy synthetic chat load: shared resource budget", fontsize=14
    )
    fig.savefig(output, dpi=160)
    plt.close(fig)
    if output.suffix == ".svg":
        output.write_text(
            "\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plot", type=Path)
    args = parser.parse_args()
    results = [condense(path) for path in args.receipts]
    args.output.write_text(
        json.dumps(
            {
                "scope": "benchmark-only policy model; no production activation",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    if args.plot:
        plot(results, args.plot)
    for row in results:
        print(
            json.dumps(
                {
                    key: row[key]
                    for key in (
                        "policy",
                        "chats",
                        "load_wall_s",
                        "steady_compute_cpus",
                        "ordinary_load_reads_ms",
                        "max_compute_mib_during_load",
                        "max_compute_pids_during_load",
                    )
                }
            )
        )


if __name__ == "__main__":
    main()
