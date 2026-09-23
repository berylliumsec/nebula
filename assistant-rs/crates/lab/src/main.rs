//! Development CLI only; intentionally has no `serve`, tool or provider command.

use clap::{Parser, Subcommand};
use nebula_assistant_domain::AppendEvent;
use nebula_assistant_storage::{Config, Error, Journal};
use serde_json::json;
use std::{
    path::PathBuf,
    time::{Duration, Instant},
};

#[derive(Parser)]
#[command(
    about = "Isolated Rust assistant journal laboratory; NOT the Nebula assistant replacement"
)]
struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Generate a new isolated fixture; refuses an existing directory.
    Fixture {
        #[arg(long)]
        directory: PathBuf,
        #[arg(long, default_value_t = 10_000, value_parser = clap::value_parser!(u32).range(1..=10_000))]
        turns: u32,
        #[arg(long, default_value_t = 100, value_parser = clap::value_parser!(u32).range(1..=1000))]
        events_per_turn: u32,
    },
    /// Measure journal append/replay only. Does not measure real agents or APIs.
    Measure {
        #[arg(long)]
        directory: PathBuf,
        #[arg(long, default_value_t = 100, value_parser = clap::value_parser!(u32).range(1..=200))]
        streams: u32,
        #[arg(long, default_value_t = 20, value_parser = clap::value_parser!(u32).range(1..=10_000))]
        events_per_stream: u32,
    },
    /// Replay only a laboratory journal, never a Nebula database.
    Replay {
        #[arg(long)]
        database: PathBuf,
        #[arg(long)]
        turn: String,
        #[arg(long, default_value_t = 0)]
        after: i64,
    },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    match Args::parse().command {
        Command::Fixture {
            directory,
            turns,
            events_per_turn,
        } => {
            private_directory(&directory)?;
            let journal =
                Journal::create(&directory.join("assistant-lab.db"), Config::default()).await?;
            for turn in 0..turns {
                for index in 0..events_per_turn {
                    journal.append(event(turn, index)).await?;
                }
                if (turn + 1) % 1000 == 0 {
                    eprintln!("Created {} of {turns} fixture turns", turn + 1);
                }
            }
            journal.shutdown().await?;
            let receipt = json!({"format": "nebula.assistant-fixture/v1", "turns": turns,
                "events_per_turn": events_per_turn, "events": u64::from(turns) * u64::from(events_per_turn),
                "content_seed": "assistant-fixture-v1", "production_compatible": false,
                "note": "Deterministic event content and layout; event UUIDs and timestamps are generated."});
            save_receipt(&directory, &receipt)?;
        }
        Command::Measure {
            directory,
            streams,
            events_per_stream,
        } => {
            private_directory(&directory)?;
            let journal =
                Journal::create(&directory.join("assistant-lab.db"), Config::default()).await?;
            let started = Instant::now();
            let mut tasks = tokio::task::JoinSet::new();
            for turn in 0..streams {
                let journal = journal.clone();
                tasks.spawn(async move {
                    let mut latencies = Vec::new();
                    let mut rejections = 0u64;
                    for index in 0..events_per_stream {
                        let start = Instant::now();
                        loop {
                            match journal.append(event(turn, index)).await {
                                Ok(_) => break,
                                Err(Error::Capacity)
                                    if start.elapsed() < Duration::from_secs(10) =>
                                {
                                    rejections += 1;
                                    tokio::time::sleep(Duration::from_millis(1)).await;
                                }
                                Err(error) => return Err(error),
                            }
                        }
                        latencies.push(start.elapsed().as_micros() as u64);
                    }
                    Ok((latencies, rejections))
                });
            }
            let mut latencies = Vec::new();
            let mut rejections = 0u64;
            while let Some(result) = tasks.join_next().await {
                let (values, rejected) = result??;
                latencies.extend(values);
                rejections += rejected;
            }
            let seconds = started.elapsed().as_secs_f64();
            latencies.sort_unstable();
            let append_p99 = percentile(&latencies, 99);
            let mut replay_latencies = Vec::new();
            let mut replayed = 0usize;
            for turn in 0..streams {
                let mut after = 0;
                while after < i64::from(events_per_stream) {
                    let start = Instant::now();
                    let events = journal
                        .replay(&format!("fixture-turn-{turn:05}"), after)
                        .await?;
                    let Some(last) = events.last() else {
                        return Err("replay ended before all committed events".into());
                    };
                    after = last.sequence;
                    replayed += events.len();
                    replay_latencies.push(start.elapsed().as_micros() as u64);
                }
            }
            replay_latencies.sort_unstable();
            if replayed != latencies.len() {
                return Err("replay count differs from committed event count".into());
            }
            journal.shutdown().await?;
            let receipt = json!({
                "format": "nebula.assistant-journal-measurement/v1",
                "scope": "synthetic journal only; no API, provider, harness, tools, or real agents",
                "streams": streams, "events_per_stream": events_per_stream,
                "committed_events": latencies.len(), "replayed_events": replayed,
                "elapsed_seconds": seconds, "events_per_second": latencies.len() as f64 / seconds,
                "append_p50_us": percentile(&latencies, 50), "append_p95_us": percentile(&latencies, 95),
                "append_p99_us": append_p99, "replay_batch_p99_us": percentile(&replay_latencies, 99),
                "capacity_rejections_retried": rejections, "peak_rss_kib": peak_rss(),
                "sqlite": {"journal_mode": "wal", "synchronous": "normal", "writer_capacity": 128, "readers": 4},
                "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" },
                "package_version": env!("CARGO_PKG_VERSION"),
                "python_baseline_measured": false, "assistant_performance_gates_verified": false
            });
            save_receipt(&directory, &receipt)?;
        }
        Command::Replay {
            database,
            turn,
            after,
        } => {
            let journal = Journal::open(&database, Config::default()).await?;
            let events = journal.replay(&turn, after).await?;
            println!("{}", serde_json::to_string(&events)?);
            journal.shutdown().await?;
        }
    }
    Ok(())
}

fn event(turn: u32, index: u32) -> AppendEvent {
    AppendEvent {
        turn_id: format!("fixture-turn-{turn:05}"),
        event_type: "delta".into(),
        payload:
            json!({"text": format!("fixture assistant content {turn}/{index}"), "index": index})
                .as_object()
                .unwrap()
                .clone(),
        actor_id: None,
        idempotency_key: Some(format!("fixture:{index}")),
    }
}

fn private_directory(path: &std::path::Path) -> std::io::Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}

fn percentile(sorted: &[u64], percent: usize) -> u64 {
    sorted[(sorted.len() * percent).div_ceil(100).saturating_sub(1)]
}

fn peak_rss() -> Option<u64> {
    std::fs::read_to_string("/proc/self/status")
        .ok()?
        .lines()
        .find_map(|line| {
            line.strip_prefix("VmHWM:")?
                .split_whitespace()
                .next()?
                .parse()
                .ok()
        })
}

fn save_receipt(
    directory: &std::path::Path,
    receipt: &serde_json::Value,
) -> Result<(), Box<dyn std::error::Error>> {
    let formatted = serde_json::to_string_pretty(receipt)?;
    std::fs::write(directory.join("receipt.json"), format!("{formatted}\n"))?;
    println!("{formatted}");
    Ok(())
}
