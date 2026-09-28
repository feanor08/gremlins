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
