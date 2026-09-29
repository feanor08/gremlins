from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_analyzer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "analyze_benchmark_traces.py"
    spec = spec_from_file_location("analyze_benchmark_traces", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summarize_accepts_partial_b_only_study():
    analyzer = _load_analyzer()
    summary = analyzer.summarize([
        {
            "arm": "B",
            "num_turns": 4,
            "toolsearch_calls": 0,
            "permission_denials": [],
            "post_gremlins_verification_calls": 0,
            "tool_sequence": ["Bash", "Bash"],
        }
    ])

    assert summary["B"]["runs"] == 1
    assert summary["C"]["runs"] == 0
    assert summary["C"]["toolsearch_calls"] == 0
    assert summary["C"]["gremlins_client_result_chars_total"] == 0
    assert summary["C"]["runs_with_post_gremlins_verification"] == 0


def test_analyze_trace_exposes_repo_explorer_input_and_result(tmp_path: Path):
    analyzer = _load_analyzer()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join([
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tool-1","name":"mcp__gremlins__repo_explorer","input":{"task":"Find gate","terms":["Gate"],"symbols":[],"mode":"auto"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"tool-1","content":"{\\\"files\\\":[{\\\"path\\\":\\\"architecture.md\\\",\\\"hits\\\":[{\\\"line\\\":1733,\\\"text\\\":\\\"Gate: Phase Delta must pass first.\\\"}]}]}"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"num_turns":2,"result":"done","usage":{"input_tokens":1,"output_tokens":1}}',
        ]) + "\n",
        encoding="utf-8",
    )

    row = analyzer.analyze_trace(trace)
    assert row["gremlins_terms"] == ["Gate"]
    assert row["gremlins_symbols"] == []
    assert row["gremlins_input"]["task"] == "Find gate"
    assert "Phase Delta" in row["gremlins_result_excerpt"]
    assert row["returned_paths"] == ["architecture.md"]


def test_analyze_trace_exposes_iterative_evidence_pack_and_fallback(tmp_path: Path):
    analyzer = _load_analyzer()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join([
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"g1","name":"mcp__gremlins__evidence_pack","input":{"task":"Find implementation","detail":"broad"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"g1","content":"{\\\"request\\\":{\\\"detail\\\":\\\"broad\\\",\\\"result_budget_chars\\\":8000},\\\"files\\\":[{\\\"path\\\":\\\"architecture.md\\\"}],\\\"related_paths\\\":[{\\\"path\\\":\\\"src/gremlins/retrieval.py\\\"}]}"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"g2","name":"mcp__gremlins__evidence_pack","input":{"task":"Inspect effective_terms","detail":"focused","paths":["src/gremlins/retrieval.py"],"symbols":["effective_terms"]}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"g2","content":"{\\\"request\\\":{\\\"detail\\\":\\\"focused\\\",\\\"result_budget_chars\\\":3600},\\\"files\\\":[{\\\"path\\\":\\\"src/gremlins/retrieval.py\\\"}]}"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"r1","name":"Read","input":{"file_path":"src/gremlins/retrieval.py"}}]}}',
            '{"type":"result","subtype":"success","is_error":false,"num_turns":4,"result":"done","usage":{"input_tokens":1,"output_tokens":1}}',
        ]) + "\n",
        encoding="utf-8",
    )

    row = analyzer.analyze_trace(trace)
    assert row["evidence_pack_calls"] == 2
    assert row["evidence_pack_details"] == ["broad", "focused"]
    assert row["gremlins_call_details"][1]["symbols"] == ["effective_terms"]
    assert "src/gremlins/retrieval.py" in row["returned_paths"]
    assert row["direct_evidence_calls"] == 1
    assert row["direct_evidence_after_final_gremlins"] == 1
    assert row["after_final_gremlins_uses"][0]["name"] == "Read"
    assert row["redundant_post_gremlins_calls"] == 1
    assert row["redundant_post_gremlins_uses"][0]["gremlins_returned_paths_touched"] == [
        "src/gremlins/retrieval.py"
    ]
