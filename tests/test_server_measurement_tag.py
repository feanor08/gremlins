from gremlins.server import _effective_measurement_tag


def test_benchmark_measurement_tag_env_overrides_caller(monkeypatch):
    monkeypatch.setenv("GREMLINS_MEASUREMENT_TAG", "benchmark:authoritative")
    assert _effective_measurement_tag("caller:wrong") == "benchmark:authoritative"


def test_explicit_measurement_tag_used_without_benchmark_env(monkeypatch):
    monkeypatch.delenv("GREMLINS_MEASUREMENT_TAG", raising=False)
    assert _effective_measurement_tag("caller:tag") == "caller:tag"
    assert _effective_measurement_tag(None) is None
