"""The retention eval's pure parts: scenario generation, scoring and reporting.

The eval itself drives a real Core; these tests keep its measuring stick honest
without a network or a model.
"""

import re

import pytest

from scripts.context_retention_eval import (
    Pricing,
    Probe,
    Scenario,
    TurnSpec,
    aggregate,
    answer_lines,
    build_s1,
    build_s2,
    build_s3,
    build_scenario,
    classify,
    estimate_cost,
    foreign_projects,
    parse_args,
    pricing_from_descriptor,
    produced_ids,
    render_markdown,
    run_metrics,
    score_turn,
    summarize,
    tool_call_stats,
    unanswered,
    _literal,
    _number,
)

PRICING = Pricing(input=1.0, output=2.0, cached_input=0.1, source="test")


def _role(scenario, role):
    return next(turn for turn in scenario.turns if turn.role == role)


def _notes(scenario):
    return [turn for turn in scenario.turns if turn.role in {"plant", "filler"}]


@pytest.mark.parametrize("name", ["s1", "s2", "s3"])
def test_a_seed_always_generates_the_same_scenario(name):
    first = build_scenario(name, 7, nonce="n1")
    again = build_scenario(name, 7, nonce="n1")
    other = build_scenario(name, 8, nonce="n1")

    assert first == again
    assert [turn.content for turn in first.turns] != [
        turn.content for turn in other.turns
    ]
    assert [probe.expected for probe in first.probes] != [
        probe.expected for probe in other.probes
    ]


@pytest.mark.parametrize("name", ["s1", "s2", "s3"])
def test_the_run_nonce_only_changes_the_first_message(name):
    first = build_scenario(name, 7, nonce="aaaaaa")
    second = build_scenario(name, 7, nonce="bbbbbb")

    assert "aaaaaa" in first.turns[0].content
    assert first.turns[0].content.replace("aaaaaa", "bbbbbb") == second.turns[0].content
    assert first.turns[1:] == second.turns[1:]
    assert first.probes == second.probes
    assert first.files == second.files


def test_s1_plants_every_fact_once_in_filler_and_asks_for_all_of_them():
    scenario = build_s1(11, turn_chars=3_000)
    plants = [turn for turn in scenario.turns if turn.role == "plant"]
    probe_turn = _role(scenario, "probe")

    assert len(scenario.probes) == 12
    assert len({probe.expected for probe in scenario.probes}) == 12
    assert len(plants) == 13  # twelve facts and one correction
    assert [turn.role for turn in scenario.turns[-4:]] == [
        "filler",
        "filler",
        "probe",
        "probe_retry",
    ]
    assert probe_turn.probes == tuple(probe.id for probe in scenario.probes)
    for index, probe in enumerate(scenario.probes, start=1):
        assert f"Q{index}: {probe.question}" in probe_turn.content
        # The corrected fact is planted with its superseded value.
        planted_value = probe.forbidden or probe.patterns
        carriers = [
            number
            for number, turn in enumerate(_notes(scenario), start=1)
            if all(re.search(pattern, turn.content, re.I) for pattern in planted_value)
        ]
        # Only the planted turn (and, for a port, its correction) carries the
        # value; filler never does.
        assert carriers == ([3, 8] if probe.forbidden else [probe.planted_at])
    for turn in _notes(scenario):
        assert 2_500 < len(turn.content) < 4_000
    retry = _role(scenario, "probe_retry")
    assert (retry.tools, retry.probes) == (True, probe_turn.probes)
    assert not probe_turn.tools


def test_s1_supersedes_a_fact_after_it_was_planted():
    scenario = build_s1(11)
    port = scenario.probe("s1.exporter_port")
    correction = scenario.turns[7].content

    assert port.planted_at == 3
    assert port.forbidden
    assert re.search(port.forbidden[0], scenario.turns[2].content)
    assert re.search(port.patterns[0], correction)
    assert re.search(port.forbidden[0], correction)
    assert not re.search(port.patterns[0], scenario.turns[2].content)


def test_filler_carries_no_digits_that_could_answer_a_probe():
    scenario = build_s1(3)
    for turn in _notes(scenario):
        text = turn.content
        for probe in scenario.probes:
            for pattern in probe.patterns:
                text = re.sub(pattern, "", text, flags=re.I)
            for pattern in probe.forbidden:
                text = re.sub(pattern, "", text, flags=re.I)
        assert not re.search(r"\d", text)


def test_s2_files_form_one_chain_that_only_reveals_the_next_path():
    scenario = build_s2(5, files=6)
    paths = list(scenario.files)

    assert len(paths) == 6 == len(scenario.probes)
    assert paths[0] in scenario.turns[0].content
    assert all(path not in scenario.turns[0].content for path in paths[1:])
    for index, path in enumerate(paths, start=1):
        body = scenario.files[path]
        assert body.startswith(f"This is file {index} of the chain.")
        next_line = body.strip().splitlines()[-1]
        assert next_line == (f"NEXT: {paths[index]}" if index < 6 else "NEXT: END")
        probe = scenario.probes[index - 1]
        assert probe.planted_at == index
        assert f"KEY: {probe.expected}" in body
        # The key sits mid-file, below the first line a receipt might quote.
        assert body.index("KEY:") > 200
    assert len({probe.expected for probe in scenario.probes}) == 6
    with pytest.raises(ValueError):
        build_s2(5, files=2)


def test_s3_asks_about_distinct_files_with_and_without_tools():
    scenario = build_s3(9)
    load, no_tools, tools = scenario.turns

    assert (load.role, load.tools, load.probes) == ("load", True, ())
    assert (no_tools.tools, len(no_tools.probes)) == (False, 2)
    assert (tools.tools, len(tools.probes)) == (True, 1)
    asked = {*no_tools.probes, *tools.probes}
    assert len(asked) == 3
    for probe in scenario.probes:
        holders = [
            path
            for path, body in scenario.files.items()
            if all(re.search(pattern, body, re.I) for pattern in probe.patterns)
        ]
        assert len(holders) == 1
        assert holders[0] in probe.question
        assert all(path in load.content for path in scenario.files)


def test_answer_lines_reads_common_numbered_formats():
    text = "\n".join(
        [
            "Here you go:",
            "Q1: ZETA-4417",
            "**Q2:** /srv/a/b.yaml",
            "- Q3. 30123",
            "q4) 10.1.2.3",
            "Q5 – Marisol",
            "  Okafor",
            "",
            "Q1: a later recap does not overwrite the first answer",
        ]
    )

    assert answer_lines(text) == {
        1: "ZETA-4417",
        2: "/srv/a/b.yaml",
        3: "30123",
        4: "10.1.2.3",
        5: "Marisol Okafor",
    }


def test_answer_lines_reads_bare_numbers_and_tables_for_the_chain():
    prefix = r"(?:file\s*#?)?"
    text = "1: ALPHA-1111\nFile 2: BRAVO-2222\n| 3 | DELTA-3333 |\n**4.** ECHO-4444"

    assert answer_lines(text, prefix) == {
        1: "ALPHA-1111",
        2: "BRAVO-2222",
        3: "DELTA-3333",
        4: "ECHO-4444",
    }
    # With a required prefix an IP at the start of a line is not an answer number.
    assert answer_lines("10.4.2.1 is the host") == {}


def _probe(**overrides):
    values = dict(
        id="p",
        kind="port",
        question="?",
        expected="30123",
        patterns=(_number("30123"),),
    )
    return Probe(**{**values, **overrides})


def test_classify_separates_correct_stale_unknown_wrong_and_missing():
    corrected = _probe(forbidden=(_number("30999"),))

    assert classify(corrected, "It moved to 30123.") == "correct"
    assert classify(corrected, "30123 (previously 30999)") == "correct"
    assert classify(corrected, "Port 30999.") == "stale"
    assert classify(corrected, "Unknown") == "unknown"
    assert classify(corrected, "I don't know.") == "unknown"
    assert classify(corrected, "Port 8080") == "wrong"
    assert classify(corrected, "  ") == "missing"


def test_patterns_match_whole_values_only():
    assert re.search(_number("10.4.2.1"), "host 10.4.2.1.")
    assert not re.search(_number("10.4.2.1"), "host 10.4.2.15")
    assert not re.search(_number("10.4.2.1"), "host 110.4.2.1")
    assert re.search(_number("34,000"), "$34000 cap")
    assert re.search(_number("34,000"), "34 000 dollars")
    assert not re.search(_number("34,000"), "134,000")
    assert re.search(_number("4.12.7"), "use v4.12.7")
    assert not re.search(_number("4.12.7"), "4.12.71")
    assert re.search(_literal("ZETA-4417"), "ticket `zeta-4417`.", re.I)
    assert not re.search(_literal("ZETA-4417"), "ZETA-44170")
    assert re.search(_literal("/srv/a/m-2.yaml"), "at /srv/a/m-2.yaml.")


def test_score_turn_scores_each_numbered_line_strictly_and_the_reply_loosely():
    scenario = build_s1(4)
    probe_turn = _role(scenario, "probe")
    first, second = scenario.probes[:2]
    answer = f"Q1: {second.expected}\nQ2: {first.expected}\nQ3: unknown"

    results = score_turn(scenario, probe_turn, answer)

    assert len(results) == 12
    assert [item["outcome"] for item in results[:3]] == ["wrong", "wrong", "unknown"]
    assert [item["mentioned"] for item in results[:3]] == [True, True, False]
    assert all(item["outcome"] == "missing" for item in results[3:])


def test_score_turn_uses_the_whole_reply_for_a_single_value():
    scenario = build_s3(2)
    turn = scenario.turns[2]
    probe = scenario.probe(turn.probes[0])

    [result] = score_turn(scenario, turn, f"The path is `{probe.expected}`.")

    assert result["correct"] and result["mentioned"]


def test_tool_call_stats_counts_rereads_and_receipt_refetches():
    files = ["chain/a.txt", "chain/b.txt"]
    first = tool_call_stats(
        [
            {"name": "workspace.read", "arguments": {"path": "chain/a.txt"}},
            {"name": "workspace.read", "arguments": {"path": "./chain/b.txt"}},
            {"name": "workspace.read", "arguments": {"path": "chain/a.txt"}},
            {
                "name": "workspace.read",
                "arguments": {"path": "chain/b.txt", "starting_line": 20},
            },
            {"name": "tool_output.read", "arguments": {"artifact_id": "x"}},
        ],
        files,
    )

    assert first["tool_calls"] == 5
    assert first["source_reads"] == 4
    assert first["repeated_source_reads"] == 1
    assert first["receipt_refetches"] == 1
    assert first["by_name"] == {"tool_output.read": 1, "workspace.read": 4}

    later = tool_call_stats(
        [
            {"name": "run_command", "arguments": {"command": "cat chain/b.txt"}},
            {"name": "tool_output.search", "arguments": {"query": "KEY"}},
        ],
        files,
        first["read_markers"],
    )

    assert later["repeated_source_reads"] == 1
    assert later["receipt_refetches"] == 1


def test_pricing_counts_cached_input_at_its_own_rate():
    usage = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 400_000,
        "output_tokens": 500_000,
    }

    assert PRICING.cost(usage) == pytest.approx(0.6 + 0.04 + 1.0)
    assert PRICING.cost(None) == 0

    parsed = pricing_from_descriptor(
        {
            "pricing": {
                "prompt": "0.000000035",
                "completion": "0.00000029",
                "input_cache_read": "0.000000001",
            }
        }
    )
    assert parsed is not None
    assert (parsed.input, parsed.output, parsed.cached_input) == pytest.approx(
        (0.035, 0.29, 0.001)
    )
    assert pricing_from_descriptor({"pricing": {"prompt": "0.000001"}}) is None
    no_cache_rate = pricing_from_descriptor(
        {"pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    )
    assert no_cache_rate is not None and no_cache_rate.cached_input == pytest.approx(
        1.0
    )


def test_estimate_is_an_upper_bound_per_call_and_repeat():
    scenario = Scenario(
        name="s1", title="t", profile="s1", turns=(), probes=(), expected_calls=10
    )

    once = estimate_cost([scenario], {"s1": 10_000}, PRICING, 1)

    assert once == pytest.approx((10 * 10_000 * 1.0 + 10 * 800 * 2.0) / 1_000_000)
    assert estimate_cost([scenario], {"s1": 10_000}, PRICING, 3) == pytest.approx(
        3 * once
    )


def _turn(index, role, **values):
    return {"index": index, "role": role, "ok": True, "elapsed_s": 2.0, **values}


def test_run_metrics_counts_unasked_probes_as_misses_and_reads_the_context():
    scenario = build_s1(4)
    first = scenario.probes[0]
    turns = [
        _turn(
            1,
            "plant",
            usage={
                "input_tokens": 1_000,
                "output_tokens": 10,
                "cached_input_tokens": 0,
            },
            context={"snapshot": None, "compacted_through": 0},
        ),
        _turn(
            2,
            "plant",
            usage={
                "input_tokens": 3_000,
                "output_tokens": 10,
                "cached_input_tokens": 1_000,
            },
            context_usage={"input_tokens": 5_000, "output_tokens": 300},
            context={
                "compacted_through": 4,
                "snapshot": {
                    "id": "b",
                    "version": 2,
                    "status": "ready",
                    "quality": "salvaged",
                },
                "last_provider_request": {
                    "estimated_total": 4_000,
                    "reported_input_tokens": 3_000,
                },
            },
        ),
        _turn(
            3,
            "probe",
            usage={
                "input_tokens": 1_000,
                "output_tokens": 50,
                "cached_input_tokens": 1_000,
            },
            context={
                "snapshot": {"id": "a", "version": 1, "status": "failed"},
                "estimate_calibration": 0.62,
            },
            scores=score_turn(
                scenario, _role(scenario, "probe"), f"Q1: {first.expected}"
            ),
        ),
    ]
    turns[2]["ok"] = False

    result = run_metrics(scenario, turns, PRICING)
    metrics = result["metrics"]

    assert metrics["probes"] == 12
    assert metrics["recall"] == pytest.approx(1 / 12, abs=1e-4)
    assert metrics["recall_early"] == pytest.approx(1 / 5)  # planted on turns 1-5
    assert metrics["compactions"] == 2
    assert metrics["failed_compactions"] == 1
    assert metrics["snapshot_qualities"] == ["salvaged"]
    assert metrics["compacted_through"] == 4
    assert metrics["failed_turns"] == 1
    assert metrics["input_tokens"] == 5_000
    assert metrics["cache_hit_ratio"] == pytest.approx(0.4)
    assert metrics["compaction_input_tokens"] == 5_000
    assert metrics["estimate_ratio"] == pytest.approx(0.75)
    assert metrics["estimate_calibration"] == 0.62
    assert metrics["wall_seconds"] == 6.0
    expected_cost = PRICING.cost(
        {"input_tokens": 5_000, "cached_input_tokens": 2_000, "output_tokens": 70}
    ) + PRICING.cost({"input_tokens": 5_000, "output_tokens": 300})
    assert metrics["cost_usd"] == pytest.approx(expected_cost, abs=1e-6)

    unasked = build_s1(4)
    only_setup = run_metrics(unasked, turns[:1], PRICING)
    assert {item["outcome"] for item in only_setup["probe_results"]} == {"not_asked"}
    assert only_setup["metrics"]["recall"] == 0


def test_run_metrics_splits_s3_recall_by_tool_availability():
    scenario = build_s3(6)
    load, no_tools, tools = scenario.turns
    [asked_with_tools] = tools.probes
    with_tools = scenario.probe(asked_with_tools)
    turns = [
        _turn(
            1, "load", answer="READY", tool_stats=tool_call_stats([], scenario.files)
        ),
        _turn(
            2,
            "probe_no_tools",
            scores=score_turn(scenario, no_tools, "Q1: unknown\nQ2: unknown"),
        ),
        _turn(
            3,
            "probe_tools",
            scores=score_turn(scenario, tools, with_tools.expected),
            tool_stats={
                "tool_calls": 1,
                "repeated_source_reads": 1,
                "receipt_refetches": 0,
            },
        ),
    ]

    metrics = run_metrics(scenario, turns, PRICING)["metrics"]

    assert metrics["recall_no_tools"] == 0
    assert metrics["recall_tools"] == 1
    # The fourth value is a distractor no turn asks about: not scored.
    assert metrics["recall"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["probes"] == 3
    assert metrics["repeated_source_reads"] == 1
    assert metrics["load_turn_leaked_values"] == 0


def test_summary_and_markdown_report_mean_and_min_across_repeats():
    runs = [
        {
            "scenario": "s1",
            "repeat": repeat,
            "metrics": {
                "recall": recall,
                "compactions": 3,
                "cost_usd": 0.01,
                "snapshot_qualities": [],
            },
            "probe_results": [
                {
                    "probe": "s1.ticket",
                    "kind": "identifier",
                    "planted_at": 1,
                    "correct": recall > 0.5,
                    "outcome": "correct" if recall > 0.5 else "wrong",
                }
            ],
            "turns": [
                {
                    "index": 4,
                    "role": "plant",
                    "ok": repeat == 1,
                    "error": 'HTTP 503: {"detail":"compaction failed","code":"x"}',
                    "refused_attempts": 1,
                    "refusals": ['HTTP 503: {"detail":"summary invalid"}'],
                },
                {"index": 5, "role": "filler", "ok": False, "skipped": True},
            ],
        }
        for repeat, recall in ((1, 0.75), (2, 0.25))
    ]

    summary = summarize(runs)

    assert summary["s1"]["metrics"]["recall"] == {
        "mean": 0.5,
        "min": 0.25,
        "max": 0.75,
        "n": 2,
    }
    assert summary["s1"]["probes"]["s1.ticket"]["correct"] == 1
    assert summary["s1"]["probes"]["s1.ticket"]["outcomes"] == {
        "correct": 1,
        "wrong": 1,
    }

    markdown = render_markdown(
        {
            "model": "m",
            "seed": 1,
            "repeat": 2,
            "profiles": {
                "s1": {
                    "context_window": 12_000,
                    "max_output_tokens": 1_024,
                    "resolved": {"target_input_tokens": 8_232},
                }
            },
            "pricing": {"source": "test"},
            "summary": summary,
            "runs": runs,
        }
    )

    assert "| s1 | 50% (min 25%) |" in markdown
    assert "| – / – |" in markdown  # no failed-turn or refusal counts recorded
    assert "12,000 / 1,024 → 8,232" in markdown
    assert (
        "s1 repeat 1: plant turn 4 succeeded after 1 refused send(s): summary invalid"
        in markdown
    )
    assert "s1 repeat 2: plant turn 4 failed: compaction failed\n" in markdown
    assert "s1 repeat 2: 1 later turn(s) skipped" in markdown
    assert "| s1.ticket | identifier | 1 | 1/2 | correct 1, wrong 1 |" in markdown


def test_aggregate_ignores_missing_values():
    assert aggregate([None, None]) is None
    assert aggregate([1, None, 3]) == {"mean": 2.0, "min": 1.0, "max": 3.0, "n": 2}


def test_arguments_require_one_core_target_and_known_scenarios(tmp_path):
    out = str(tmp_path / "report")
    args = parse_args(
        ["--base-url", "http://127.0.0.1:1", "--out", out, "--scenarios", "s2,s1"]
    )

    assert args.scenarios == ["s2", "s1"]
    assert args.workdir == f"{out}-workspaces"
    assert re.fullmatch(r"[0-9a-f]{6}", args.nonce)
    with pytest.raises(SystemExit):
        parse_args(["--out", out])
    with pytest.raises(SystemExit):
        parse_args(["--serve-from", ".", "--out", out])
    with pytest.raises(SystemExit):
        parse_args(["--base-url", "x", "--out", out, "--scenarios", "s9"])


def test_turn_spec_defaults_describe_a_tool_free_unscored_turn():
    turn = TurnSpec(role="filler", content="x")

    assert (turn.tools, turn.probes, turn.line_prefix) == (False, (), None)


def test_store_snapshots_count_failed_compactions_the_api_never_showed():
    scenario = build_s1(4)
    turns = [
        _turn(
            1,
            "plant",
            context={"snapshot": {"id": "c", "version": 3, "status": "ready"}},
        ),
        {
            "index": 2,
            "role": "plant",
            "ok": False,
            "refused_attempts": 3,
            "refusals": ["HTTP 503"] * 3,
            "elapsed_s": 9.0,
        },
        {"index": 3, "role": "filler", "ok": False, "skipped": True, "elapsed_s": 0},
        _turn(4, "probe", checkpoints=[]),
    ]
    snapshots = [
        {
            "id": "a",
            "version": 1,
            "status": "failed",
            "usage": {"input_tokens": 8_000, "output_tokens": 600},
        },
        {
            "id": "b",
            "version": 2,
            "status": "failed",
            "usage": {"input_tokens": 8_000, "output_tokens": 600},
        },
        {
            "id": "c",
            "version": 3,
            "status": "ready",
            "quality": "degraded",
            "usage": {"input_tokens": 7_000, "output_tokens": 300},
        },
        {
            "id": "d",
            "version": 4,
            "status": "failed",
            "usage": {"input_tokens": 9_000, "output_tokens": 600},
        },
    ]

    from_api = run_metrics(scenario, turns, PRICING)["metrics"]
    from_store = run_metrics(scenario, turns, PRICING, snapshots)["metrics"]

    assert (from_api["compactions"], from_api["failed_compactions"]) == (3, 0)
    assert from_api["compaction_input_tokens"] == 0
    assert (from_store["compactions"], from_store["failed_compactions"]) == (4, 3)
    assert from_store["snapshot_qualities"] == ["degraded"]
    assert from_store["compaction_input_tokens"] == 32_000
    assert from_store["compaction_output_tokens"] == 2_100
    assert from_store["cost_usd"] == pytest.approx(
        PRICING.cost({"input_tokens": 32_000, "output_tokens": 2_100})
    )
    assert from_store["failed_turns"] == 2
    assert from_store["skipped_turns"] == 1
    assert from_store["refused_attempts"] == 3
    assert from_store["checkpoints"] == 0
    assert "checkpoints" not in run_metrics(scenario, turns[:1], PRICING)["metrics"]


def test_cache_writes_are_part_of_input_and_priced_at_their_own_rate():
    anthropic = pricing_from_descriptor(
        {
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000005",
                "input_cache_read": "0.0000001",
                "input_cache_write": "0.00000125",
            }
        }
    )
    usage = {
        "input_tokens": 10_000,
        "cached_input_tokens": 6_000,
        "cache_creation_input_tokens": 3_000,
        "output_tokens": 100,
    }

    assert anthropic is not None and anthropic.cache_write == pytest.approx(1.25)
    assert anthropic.cost(usage) == pytest.approx(
        (1_000 * 1.0 + 6_000 * 0.1 + 3_000 * 1.25 + 100 * 5.0) / 1_000_000
    )
    # Without a write price, writes cost what uncached input costs.
    assert PRICING.cost(usage) == pytest.approx(
        (4_000 * 1.0 + 6_000 * 0.1 + 100 * 2.0) / 1_000_000
    )

    scenario = build_s3(1)
    metrics = run_metrics(scenario, [_turn(1, "load", usage=usage)], PRICING)["metrics"]
    assert metrics["cache_creation_input_tokens"] == 3_000
    assert metrics["cache_write_ratio"] == pytest.approx(0.3)
    assert metrics["cache_hit_ratio"] == pytest.approx(0.6)


def test_a_core_with_projects_the_eval_did_not_create_is_not_scratch():
    assert foreign_projects([]) == []
    assert foreign_projects([{"name": "Retention eval s1 r1 abc123"}]) == []
    assert foreign_projects(
        [
            {"name": "Retention eval s2 r1 abc123"},
            {
                "name": "Scratch Project",
                "metadata": {"created_by": "system:bootstrap"},
            },
            {"name": "Client pentest", "metadata": {"created_by": "operator"}},
            {},
        ]
    ) == ["Client pentest", "(unnamed)"]


def test_a_probe_turn_that_failed_is_scored_as_such_not_as_unasked():
    scenario = build_s2(3, files=4)
    [turn] = scenario.turns
    failed = {
        "index": 1,
        "role": "probe",
        "ok": False,
        "error": "the complete provider request exceeds the selected model context",
        "scores": unanswered(map(scenario.probe, turn.probes), "turn_failed"),
        "usage": {"input_tokens": 200_000, "cached_input_tokens": 100_000},
        "usage_source": "store",
        "elapsed_s": 100.0,
    }

    result = run_metrics(scenario, [failed], PRICING)

    assert [item["outcome"] for item in result["probe_results"]] == ["turn_failed"] * 4
    assert result["metrics"]["recall"] == 0
    assert result["metrics"]["failed_turns"] == 1
    assert result["metrics"]["input_tokens"] == 200_000
    assert result["metrics"]["cache_hit_ratio"] == 0.5


def test_the_tools_on_retry_is_scored_apart_from_tool_free_recall():
    scenario = build_s1(8)
    probe, retry = _role(scenario, "probe"), _role(scenario, "probe_retry")
    first, second = scenario.probes[:2]
    turns = [
        _turn(16, "probe", scores=score_turn(scenario, probe, "Q1: unknown")),
        _turn(
            17,
            "probe_retry",
            scores=score_turn(
                scenario, retry, f"Q1: {first.expected}\nQ2: {second.expected}"
            ),
            tool_stats=tool_call_stats(
                [
                    {"name": "conversation.search", "arguments": {"query": "ticket"}},
                    {"name": "conversation.search", "arguments": {"query": "path"}},
                    {"name": "notes.write", "arguments": {"content": "x"}},
                ]
            ),
        ),
    ]

    metrics = run_metrics(scenario, turns, PRICING)["metrics"]

    assert metrics["probes"] == 12
    assert metrics["recall"] == 0
    assert metrics["recall_retry"] == pytest.approx(2 / 12, abs=1e-4)
    assert metrics["conversation_searches"] == 2
    assert metrics["notes_writes"] == 1
    assert metrics["tool_calls_by_name"] == {"conversation.search": 2, "notes.write": 1}
    assert "recall_retry" not in run_metrics(scenario, turns[:1], PRICING)["metrics"]


def test_receipt_refetches_of_earlier_turn_outputs_are_attributed():
    earlier = produced_ids(
        {
            "tool_call_id": "call-1",
            "result_artifact_id": "art-1",
            "artifacts": [{"artifact_id": "art-2"}, "art-3", {"id": None}],
        }
    )
    assert earlier == {"call-1", "art-1", "art-2", "art-3"}
    assert produced_ids({"tool_call_id": None, "artifacts": None}) == set()

    stats = tool_call_stats(
        [
            {"name": "tool_output.search", "arguments": {"tool_call_id": "call-1"}},
            {"name": "tool_output.read", "arguments": {"artifact_id": "art-3"}},
            {"name": "tool_output.read", "arguments": {"artifact_id": "this-turn"}},
            {"name": "workspace.read", "arguments": {"artifact_id": "art-1"}},
        ],
        earlier_ids=earlier,
    )

    assert stats["receipt_refetches"] == 3
    assert stats["cross_turn_refetches"] == 2


def test_working_notes_size_is_the_last_turn_that_reported_notes():
    scenario = build_s2(2, files=3)
    turns = [
        _turn(1, "probe", context={"working_notes": {"revision": 1, "chars": 40}}),
        _turn(2, "probe", context={}),
    ]

    metrics = run_metrics(scenario, turns, PRICING)["metrics"]

    assert metrics["working_notes_chars"] == 40
    assert (
        run_metrics(scenario, turns[1:], PRICING)["metrics"]["working_notes_chars"]
        is None
    )


def test_compaction_latency_is_the_wait_before_a_compacting_turn_started():
    scenario = build_s1(5)

    def turn(index, version, prepare, refused=0):
        snapshot = {"id": f"v{version}", "version": version} if version else None
        return _turn(
            index,
            "plant",
            prepare_s=prepare,
            refused_attempts=refused,
            context={"snapshot": snapshot},
        )

    turns = [
        turn(1, 0, 0.4),
        turn(2, 1, 61.0),  # first compaction
        turn(3, 1, 0.5),  # the snapshot keeps serving
        turn(4, 1, 30.0, refused=2),  # two failed compactions, then no snapshot
        turn(5, 3, 20.0),  # compacted again
    ]
    snapshots = [
        {
            "id": "v1",
            "version": 1,
            "status": "ready",
            "quality": "complete",
            "segment_count": 2,
        },
        {"id": "v2", "version": 2, "status": "failed"},
        {
            "id": "v3",
            "version": 3,
            "status": "ready",
            "quality": "salvaged",
            "dropped_items": 2,
            "segment_count": 3,
            "reused_segments": 2,
        },
    ]

    metrics = run_metrics(scenario, turns, PRICING, snapshots)["metrics"]

    assert metrics["compaction_latency_mean"] == pytest.approx(37.0)
    assert metrics["compaction_latency_max"] == 61.0
    assert metrics["snapshot_qualities"] == ["complete", "salvaged"]
    assert metrics["snapshot_dropped_items"] == 2
    assert (metrics["segments"], metrics["reused_segments"]) == (5, 2)

    report = {
        "model": "m",
        "seed": 1,
        "repeat": 1,
        "profiles": {},
        "pricing": {},
        "summary": summarize(
            [
                {
                    "scenario": "s1",
                    "repeat": 1,
                    "metrics": metrics,
                    "probe_results": [],
                    "turns": [],
                }
            ]
        ),
    }
    markdown = render_markdown(report)
    assert "3, 1.0 failed; 37 s each (max 61 s)" in markdown
    assert "complete, salvaged; 2 dropped; reused 2 of 5 segments" in markdown
