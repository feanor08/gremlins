from pathlib import Path
import subprocess

from gremlins.benchmark import (
    BenchmarkRecord,
    Prices,
    append_record,
    assert_study_compatible,
    build_arm_prompt,
    build_benchmark_report,
    evaluate_gate,
    gremlins_stats_for_tag,
    next_missing_run,
    normalize_usage,
    parse_usage_payload,
    write_study_metadata,
)
import gremlins.benchmark as benchmark


def test_normalize_openai_style_usage():
    usage = normalize_usage(
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=200,
        input_includes_cached=True,
    )
    assert usage.uncached_input_tokens == 600
    assert usage.cache_read_tokens == 400
    assert usage.output_tokens == 200


def test_cost_uses_separate_token_categories():
    usage = normalize_usage(
        input_tokens=1000,
        cached_input_tokens=500,
        cache_write_tokens=250,
        output_tokens=200,
        input_includes_cached=False,
    )
    prices = Prices(
        uncached_input_per_million=10,
        cache_read_per_million=1,
        cache_write_per_million=12,
        output_per_million=20,
    )
    assert prices.cost_usd(usage) == 0.0175


def test_paired_report_prefers_b_to_c(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)

    b_usage = normalize_usage(input_tokens=1000, output_tokens=200)
    c_usage = normalize_usage(input_tokens=400, output_tokens=100)

    append_record(
        "pilot",
        BenchmarkRecord(
            case_id="case-1",
            arm="B",
            client="claude",
            model="frontier",
            accepted=True,
            elapsed_seconds=10,
            frontier_usage=b_usage,
            frontier_subagents=2,
            frontier_direct_tool_calls=8,
            frontier_direct_evidence_calls=7,
        ),
    )
    append_record(
        "pilot",
        BenchmarkRecord(
            case_id="case-1",
            arm="C",
            client="claude",
            model="frontier",
            accepted=True,
            elapsed_seconds=8,
            frontier_usage=c_usage,
            frontier_subagents=0,
            frontier_direct_tool_calls=2,
            frontier_direct_evidence_calls=1,
            gremlins_calls=1,
            gremlins_result_chars=3200,
            gremlins_evidence_pack_details=("broad",),
            gremlins_evidence_pack_budgets=(8000,),
        ),
    )

    report = build_benchmark_report("pilot")
    comparison = report["comparison_B_to_C"]
    assert comparison["paired_cases"] == 1
    assert comparison["frontier_processed_tokens_change_pct"] == -58.33
    assert comparison["frontier_subagents_change_pct"] == -100.0
    assert comparison["acceptance_rate_before"] == 1.0
    assert comparison["acceptance_rate_after"] == 1.0
    assert comparison["frontier_direct_tool_calls_change_pct"] == -75.0
    assert comparison["frontier_direct_evidence_calls_change_pct"] == -85.71
    assert comparison["pairs"][0]["gremlins_evidence_pack_details_after"] == ["broad"]
    assert comparison["pairs"][0]["gremlins_evidence_pack_budgets_after"] == [8000]


def test_parse_provider_usage_shapes():
    openai = parse_usage_payload(
        {
            "usage": {
                "input_tokens": 1000,
                "input_tokens_details": {"cached_tokens": 400},
                "output_tokens": 200,
            }
        },
        "openai",
    )
    assert openai.uncached_input_tokens == 600
    assert openai.cache_read_tokens == 400

    anthropic = parse_usage_payload(
        {
            "usage": {
                "input_tokens": 300,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 100,
                "output_tokens": 50,
            }
        },
        "anthropic",
    )
    assert anthropic.uncached_input_tokens == 300
    assert anthropic.cache_read_tokens == 700
    assert anthropic.cache_write_tokens == 100


def test_gate_uses_paired_b_to_c(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    for index in range(10):
        append_record(
            "gate",
            BenchmarkRecord(
                case_id=f"case-{index}",
                arm="B",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=10,
                frontier_usage=normalize_usage(input_tokens=1000, output_tokens=200),
                frontier_subagents=1,
            ),
        )
        append_record(
            "gate",
            BenchmarkRecord(
                case_id=f"case-{index}",
                arm="C",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=9,
                frontier_usage=normalize_usage(input_tokens=400, output_tokens=100),
                frontier_subagents=0,
                frontier_redid_search=False,
                gremlins_calls=1,
            ),
        )
    gate = evaluate_gate("gate")
    assert gate["pass"] is True
    assert gate["paired_cases"] == 10


def test_tagged_gremlins_stats(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GREMLINS_STATE_DIR", str(tmp_path))
    from gremlins.metrics import record

    record({
        "worker": "repo-explorer",
        "status": "complete",
        "measurement_tag": "pilot:case-1:C",
        "elapsed_seconds": 1.25,
        "result_chars": 3200,
        "usage": {"local_model_called": False},
    })
    record({
        "worker": "triage",
        "status": "complete",
        "measurement_tag": "pilot:case-1:C",
        "elapsed_seconds": 2.0,
        "result_chars": 1800,
        "usage": {"local_model_called": True},
    })
    record({
        "worker": "repo-explorer",
        "status": "complete",
        "measurement_tag": "other",
        "elapsed_seconds": 99,
        "result_chars": 9999,
        "usage": {"local_model_called": True},
    })

    stats = gremlins_stats_for_tag("pilot:case-1:C")
    assert stats["calls"] == 2
    assert stats["local_model_calls"] == 1
    assert stats["result_chars"] == 5000
    assert stats["elapsed_seconds"] == 3.25
    assert stats["workers"] == {"repo-explorer": 1, "triage": 1}
    assert stats["evidence_pack_details"] == []
    assert stats["evidence_pack_budgets"] == []


def test_tagged_evidence_pack_stats_preserve_detail_sequence(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GREMLINS_STATE_DIR", str(tmp_path))
    from gremlins.metrics import record

    record({
        "worker": "evidence-pack",
        "status": "complete",
        "measurement_tag": "loop",
        "elapsed_seconds": 1.0,
        "result_chars": 7900,
        "result_budget_chars": 8000,
        "detail": "broad",
        "usage": {"local_model_called": False},
    })
    record({
        "worker": "evidence-pack",
        "status": "complete",
        "measurement_tag": "loop",
        "elapsed_seconds": 0.5,
        "result_chars": 3400,
        "result_budget_chars": 3600,
        "detail": "focused",
        "usage": {"local_model_called": False},
    })

    stats = gremlins_stats_for_tag("loop")
    assert stats["calls"] == 2
    assert stats["evidence_pack_details"] == ["broad", "focused"]
    assert stats["evidence_pack_budgets"] == [8000, 3600]
    assert stats["result_chars"] == 11300


def test_benchmark_prompt_carries_measurement_tag():
    prompt = build_arm_prompt("repo-001", "C", repository="/tmp/example")
    assert prompt["measurement_tag"] == "pilot:repo-001:C:r1"
    assert prompt["iteration"] == 1
    assert "mcp__gremlins__evidence_pack" in prompt["prompt"]
    assert "measurement_tag='pilot:repo-001:C:r1'" in prompt["prompt"]
    assert "repository='/tmp/example'" in prompt["prompt"]
    assert "ProviderBusy" in prompt["prompt"]
    assert "status = \"busy\"" not in prompt["prompt"]
    assert "no more than four Gremlins calls" in prompt["prompt"]
    assert "name the exact repository-relative path(s)" in prompt["prompt"]


def test_c_prompt_prefers_iterative_evidence_pack_before_frontier_redo():
    prompt = build_arm_prompt("repo-001", "C", repository="/tmp/example")["prompt"]
    assert "The FIRST evidence_pack call must use detail='broad'" in prompt
    assert "Every LATER evidence_pack call must use detail='focused'" in prompt
    assert "Do not make a second broad call" in prompt
    assert "Prefer another focused evidence_pack over direct Read/Grep/Glob retrieval" in prompt
    assert "Bash, web tools, and write/edit tools are unavailable in this arm" in prompt
    assert "FRONTIER_REDO_SEARCH=true" in prompt
    assert "Gremlins supplies evidence, not root-cause or architecture conclusions" in prompt


def test_next_missing_run_prefers_b_then_c(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    first = next_missing_run("pilot")
    assert first is not None
    assert first["case"]["id"] == "repo-001"
    assert first["arm"] == "B"

    append_record(
        "pilot",
        BenchmarkRecord(
            case_id="repo-001",
            arm="B",
            client="claude",
            model="frontier",
            accepted=True,
            elapsed_seconds=1,
            frontier_usage=normalize_usage(),
        ),
    )
    second = next_missing_run("pilot")
    assert second is not None
    assert second["case"]["id"] == "repo-001"
    assert second["arm"] == "C"


def test_repeated_runs_pair_by_case_and_iteration(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    for iteration in (1, 2):
        append_record(
            "repeat",
            BenchmarkRecord(
                case_id="case-1",
                arm="B",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=10,
                frontier_usage=normalize_usage(input_tokens=1000, output_tokens=100),
                iteration=iteration,
            ),
        )
        append_record(
            "repeat",
            BenchmarkRecord(
                case_id="case-1",
                arm="C",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=8,
                frontier_usage=normalize_usage(input_tokens=500, output_tokens=50),
                frontier_redid_search=False,
                iteration=iteration,
            ),
        )
    comparison = build_benchmark_report("repeat")["comparison_B_to_C"]
    assert comparison["paired_cases"] == 2
    assert comparison["unique_cases"] == 1
    assert len(comparison["pairs"]) == 2
    assert {item["iteration"] for item in comparison["pairs"]} == {1, 2}


def test_next_missing_run_supports_repeats(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    cases = [{"id": "only", "task": "x", "terms": [], "expected_paths": []}]
    monkeypatch.setattr(benchmark, "load_pilot_cases", lambda: cases)
    for arm in ("B", "C"):
        append_record(
            "repeat-next",
            BenchmarkRecord(
                case_id="only",
                arm=arm,
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=1,
                frontier_usage=normalize_usage(),
                iteration=1,
            ),
        )
    nxt = next_missing_run("repeat-next", repeats=2)
    assert nxt is not None
    assert nxt["iteration"] == 2
    assert nxt["arm"] == "B"


def test_gate_repetitions_do_not_replace_unique_case_coverage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    for iteration in range(1, 11):
        append_record(
            "coverage",
            BenchmarkRecord(
                case_id="same-case",
                arm="B",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=10,
                frontier_usage=normalize_usage(input_tokens=1000, output_tokens=200),
                iteration=iteration,
            ),
        )
        append_record(
            "coverage",
            BenchmarkRecord(
                case_id="same-case",
                arm="C",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=8,
                frontier_usage=normalize_usage(input_tokens=400, output_tokens=100),
                frontier_redid_search=False,
                iteration=iteration,
            ),
        )
    gate = evaluate_gate("coverage")
    assert gate["pass"] is False
    assert gate["paired_cases"] == 10
    assert gate["unique_cases"] == 1
    assert any("unique paired cases" in reason for reason in gate["reasons"])


def test_gate_requires_redo_telemetry_coverage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    for index in range(10):
        append_record(
            "redo-coverage",
            BenchmarkRecord(
                case_id=f"case-{index}",
                arm="B",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=10,
                frontier_usage=normalize_usage(input_tokens=1000, output_tokens=200),
            ),
        )
        append_record(
            "redo-coverage",
            BenchmarkRecord(
                case_id=f"case-{index}",
                arm="C",
                client="claude",
                model="frontier",
                accepted=True,
                elapsed_seconds=8,
                frontier_usage=normalize_usage(input_tokens=400, output_tokens=100),
                frontier_redid_search=None,
            ),
        )
    gate = evaluate_gate("redo-coverage")
    assert gate["pass"] is False
    assert any("redo telemetry coverage" in reason for reason in gate["reasons"])


def test_study_provenance_cannot_be_mixed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(benchmark, "benchmark_root", lambda: tmp_path)
    write_study_metadata("stable", {
        "client": "claude",
        "client_version": "1.0",
        "requested_model": "model-a",
        "source_head": "source-1",
        "gremlins_head": "gremlins-1",
        "include_a": False,
    })
    assert_study_compatible("stable", {
        "client": "claude",
        "client_version": "1.0",
        "requested_model": "model-a",
        "source_head": "source-1",
        "gremlins_head": "gremlins-1",
        "include_a": False,
    })

    try:
        assert_study_compatible("stable", {
            "client": "claude",
            "client_version": "1.0",
            "requested_model": "model-a",
            "source_head": "source-2",
            "gremlins_head": "gremlins-1",
            "include_a": False,
        })
    except RuntimeError as exc:
        assert "cannot mix different provenance" in str(exc)
        assert "source_head" in str(exc)
    else:
        raise AssertionError("mixed source commits must be rejected")



def test_local_pilot_hides_answer_key_from_worker(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "answer.py").write_text("needle = 1\n", encoding="utf-8")
    (repo / "evals" / "pilot").mkdir(parents=True)
    (repo / "evals" / "pilot" / "cases.json").write_text('{"hidden":"answer.py"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)

    monkeypatch.setattr(
        benchmark,
        "load_pilot_cases",
        lambda: [{
            "id": "case",
            "task": "find needle",
            "terms": ["needle"],
            "expected_paths": ["src/answer.py"],
        }],
    )

    import gremlins.workers as workers

    def fake_repo_explore(repository, *args, **kwargs):
        workspace = Path(repository)
        assert not (workspace / "evals").exists()
        return {
            "status": "complete",
            "files": [{"path": "src/answer.py", "hits": [{"line": 1, "text": "needle = 1"}]}],
            "hits_returned": 1,
            "hits_ranked": 1,
            "usage": {"local_model_called": False},
        }

    monkeypatch.setattr(workers, "repo_explore", fake_repo_explore)
    report = benchmark.run_local_pilot(str(repo))
    assert report["passed"] == 1
    assert report["failed"] == 0

