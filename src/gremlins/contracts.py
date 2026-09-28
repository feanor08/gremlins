from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class Finding(BaseModel):
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    kind: Literal["observed", "inference"]


class RepoSynthesis(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_queries: list[str] = Field(default_factory=list)


class FailureGroup(BaseModel):
    name: str
    likely_cause: str
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


class TriageSynthesis(BaseModel):
    summary: str
    failure_groups: list[FailureGroup] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
