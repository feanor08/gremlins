from gremlins.server import _effective_measurement_tag


def test_benchmark_measurement_tag_env_overrides_caller(monkeypatch):
    monkeypatch.setenv("GREMLINS_MEASUREMENT_TAG", "benchmark:authoritative")
    assert _effective_measurement_tag("caller:wrong") == "benchmark:authoritative"


def test_explicit_measurement_tag_used_without_benchmark_env(monkeypatch):
    monkeypatch.delenv("GREMLINS_MEASUREMENT_TAG", raising=False)
    assert _effective_measurement_tag("caller:tag") == "caller:tag"
    assert _effective_measurement_tag(None) is None


def test_evidence_pack_mcp_result_uses_compact_text_and_preserves_structure():
    import json

    from gremlins.server import _evidence_pack_mcp_result

    payload = {
        "request": {"detail": "focused", "result_budget_chars": 3600},
        "files": [{"path": "src/gremlins/retrieval.py"}],
        "truncated": False,
    }
    result = _evidence_pack_mcp_result(payload)

    assert result.structured_content == payload
    assert len(result.content) == 1
    text = result.content[0].text
    assert text == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert json.loads(text) == payload
    assert "\n" not in text
    assert len(text) <= payload["request"]["result_budget_chars"]
