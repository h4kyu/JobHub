"""Role-type classification for the web UI's filter chips: HFT/quant, GPU, robotics, and so on.

Deterministic and free — no model calls, no re-evaluation. The rubric has no category field, so this reads
what is already stored: the title (strongest signal), the model's `fit_tags`, and its summary. Descriptions
are deliberately left out: they mention everything (a robotics posting's "nice to have: CUDA"), and scanning
~12K of them on every page load is slow.

Each posting gets exactly one type. A known trading firm is always HFT / Quant. Otherwise the title decides
whenever it matches anything, ties going to the earlier (more specific) entry in ROLE_TYPES — letting the
summary break ties sent "Compiler Engineer - AI Inference" to ML, because summaries say "AI" a lot. Only a
title that matches nothing falls back to fit_tags and summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

GENERAL = "general"
TAG_WEIGHT, SUMMARY_WEIGHT = 2, 1
MIN_FALLBACK_SCORE = 2   # with no title match, one summary mention alone is not enough to leave "general"


@dataclass(frozen=True)
class RoleType:
    key: str
    label: str
    keywords: tuple[str, ...]


# Order matters for ties: specific niches before the broad buckets they overlap with.
ROLE_TYPES: tuple[RoleType, ...] = (
    RoleType("quant", "HFT / Quant", (
        "trading", "trader", "quant", "quantitative", "hft", "high frequency", "high-frequency", "market data",
        "market making", "market-making", "order book", "matching engine", "exchange connectivity", "hedge fund",
        "prop trading", "proprietary trading")),
    RoleType("gpu", "GPU / Accelerators", (
        "gpu", "cuda", "tensorrt", "triton", "rocm", "cudnn", "accelerator", "accelerated compute", "ai compiler",
        "ai compilers", "compiler", "llvm", "mlir", "inference optimization", "kernel optimization", "gpu kernels",
        "deep learning computer architecture", "computer architecture", "ai inference", "npu", "tpu", "co design", "co-design",
        "developer technology", "hpc", "high performance computing", "high-performance computing")),
    RoleType("robotics", "Robotics / Autonomy", (
        "robotics", "robot", "robotic", "autonomy", "autonomous", "self-driving", "robotaxi", "perception",
        "motion planning", "planning & controls", "planning and controls", "controls", "slam", "localization",
        "gnc", "guidance", "navigation", "ros", "ros 2", "ros2", "isaac sim", "drone", "uav", "flight software",
        "manipulation", "humanoid", "hardware in loop", "hardware-in-the-loop", "hardware-in-loop", "hil")),
    RoleType("perf", "Performance / Optimization", (
        "performance", "optimization", "optimisation", "low latency", "low-latency", "latency", "profiling",
        "simd", "efficiency", "performance-critical", "high-performance", "throughput")),
    RoleType("ml", "ML / AI Infra", (
        "machine learning", "ml", "ai", "deep learning", "ml infrastructure", "ai infrastructure", "ai platform",
        "ml platform", "mlops", "llm", "inference", "training", "applied ml", "recommendation", "generative ai",
        "computer vision", "model serving")),
    RoleType("systems", "Systems / Infra", (
        "distributed systems", "distributed system", "infrastructure", "infra", "systems software", "system software",
        "database", "storage", "networking", "network engineer", "linux", "kernel", "operating system", "backend",
        "platform", "cloud", "sre", "site reliability", "devops", "data engineer", "data engineering",
        "data infrastructure", "large-scale systems")),
    RoleType("hardware", "Embedded / Hardware", (
        "embedded", "firmware", "fpga", "asic", "soc", "rtl", "verilog", "physical design", "chip", "silicon",
        "hardware", "battery management", "power electronics", "microcontroller", "board bring-up")),
)
LABELS: dict[str, str] = {**{t.key: t.label for t in ROLE_TYPES}, GENERAL: "General SWE"}
KEYS: tuple[str, ...] = tuple(t.key for t in ROLE_TYPES) + (GENERAL,)

# Firms where nearly every engineering role sits next to trading, whatever the title says
# ("Software Developer Intern" at DRW). Matched as a bounded substring of the company name.
QUANT_FIRMS = (
    "jane street", "drw", "hudson river trading", "hrt", "imc", "optiver", "citadel", "jump trading", "virtu",
    "tower research", "point72", "de shaw", "d. e. shaw", "d.e. shaw", "five rings", "pdt", "pdtpartners",
    "two sigma", "susquehanna", "sig", "akuna", "old mission", "radix", "xtx", "headlands", "aquatic capital",
    "hyannis port", "hpr", "belvedere", "chicago trading", "flow traders", "maven securities", "millennium",
    "balyasny", "squarepoint", "wolverine", "peak6", "arrowstreet", "renaissance technologies", "cubist", "qube",
    "voleon", "gts", "tradeweb", "sparta", "geneva trading", "vatic", "hehmeyer", "optiver", "transmarket",
    "da vinci", "mako", "quantlab", "g-research", "man group", "garda", "valkyrie", "bridgewater",
)

# Longest keyword first, so "ai inference" matches as one phrase instead of "ai" then "inference".
# Case-sensitive over lowered text, with one boundary pair around the whole alternation instead of one per
# keyword (every keyword starts and ends alphanumeric): both are several times faster in CPython, and this runs
# over every evaluated posting on each page load.
def _alternation(words) -> re.Pattern[str]:
    assert all(w[:1].isalnum() and w[-1:].isalnum() for w in words), "keywords must start and end alphanumeric"
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                      + r")(?![a-z0-9])")


_PATTERNS = [(t.key, _alternation(t.keywords)) for t in ROLE_TYPES]
_FIRMS = _alternation(QUANT_FIRMS)


def _distinct(rx: re.Pattern[str], text: str | None) -> int:
    return len(set(rx.findall(text))) if text else 0


def is_quant_firm(company: str | None) -> bool:
    return bool(company and _FIRMS.search(company.lower()))


def classify(title: str, fit_tags: list[str] | None = None, summary: str | None = None,
             company: str | None = None) -> str:
    if is_quant_firm(company):
        return "quant"
    title = (title or "").lower()
    title_hits = {key: _distinct(rx, title) for key, rx in _PATTERNS}
    if any(title_hits.values()):
        return max(KEYS[:-1], key=lambda k: (title_hits[k], -KEYS.index(k)))
    tags, summary = " | ".join(fit_tags or []).lower(), (summary or "").lower()
    fallback = {key: TAG_WEIGHT * _distinct(rx, tags) + SUMMARY_WEIGHT * _distinct(rx, summary) for key, rx in _PATTERNS}
    best = max(KEYS[:-1], key=lambda k: (fallback[k], -KEYS.index(k)))
    return best if fallback[best] >= MIN_FALLBACK_SCORE else GENERAL
