"""Pydantic models shared across the pipeline."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Bucket(StrEnum):
    likely = "likely"
    reach = "reach"
    wildcard = "wildcard"
    archive = "archive"


class AppStatus(StrEnum):
    new = "new"
    shortlisted = "shortlisted"
    applied = "applied"
    interview = "interview"
    rejected = "rejected"
    offer = "offer"
    skipped = "skipped"   # you passed on it
    closed = "closed"     # the posting is gone / no longer accepting applications


class CompanyStatus(StrEnum):
    approved = "approved"
    proposed = "proposed"
    rejected = "rejected"
    paused = "paused"


class RawJob(BaseModel):
    """A posting as returned by a source, before normalization/dedup."""

    company_name: str
    title: str
    location: str = ""
    url: str
    source: str
    external_id: str | None = None
    posted_at: str | None = None  # ISO date if known
    active: bool = True
    terms: list[str] = []
    sponsorship: str | None = None
    description_text: str | None = None  # None -> needs fetch
    raw: dict[str, Any] = Field(default_factory=dict)


class EvaluationOutput(BaseModel):
    """What the model returns for a single job (mirrors schemas/evaluation.json)."""

    job_id: int
    hard_reject_reason: str | None = None
    skills_match: int = 0
    level_match: int = 0
    work_alignment: int = 0
    experience_quality: int = 0
    interest: int = 0
    wildcard: bool = False
    wildcard_reason: str | None = None
    red_flags: list[str] = []
    fit_tags: list[str] = []   # matching characteristics, 1-3 words each ("C++17", "embedded", "hardware in loop")
    gap_tags: list[str] = []   # missing/mismatched characteristics ("CUDA", "PhD expected", "web stack")
    summary: str = ""
    application_deadline: str | None = None  # ISO date if the posting states one
    confidence: int = 50


class EvaluationBatch(BaseModel):
    evaluations: list[EvaluationOutput]


class DiscoveredCompany(BaseModel):
    name: str
    domain: str | None = None
    why_fit: str = ""
    work_areas: list[str] = []
    reputation_notes: str = ""
    tier_guess: int = 2
    fit_score: int = 50
    reputation_score: int = 50
    has_intern_program: bool | None = None
    careers_url: str | None = None
    evidence_urls: list[str] = []


class DiscoveryOutput(BaseModel):
    companies: list[DiscoveredCompany]


class ATSResolution(BaseModel):
    ats_type: str | None = None  # greenhouse | lever | ashby | workday | other | null
    ats_token: str | None = None
    careers_url: str | None = None
    notes: str = ""


class TriageResult(BaseModel):
    """Stage-1 plausibility score; see prompts/triage_system.md."""
    job_id: int
    score: int = 100
    reason: str = ""


class TriageBatch(BaseModel):
    triages: list[TriageResult] = Field(default_factory=list)
