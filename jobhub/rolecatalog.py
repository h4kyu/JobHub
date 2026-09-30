"""The shipped catalog of role types: the list a user picks their targets from.

Maintained with the code, like the company directory, and deliberately broader than any one person's
search — a user who wants frontend or security work has to find themselves in it. What a *particular*
user wants is `profile.yaml: role_types` (their picks and weights) and `excluded_role_types`; this
module only says which types exist and what words identify them.

**Order is tie-break order.** `roletype.classify` gives a posting exactly one type, and when a title
matches two, the earlier entry wins — so specific niches come before the broad buckets that overlap
them ("GPU Compiler Engineer" is GPU, not Compilers). The order of the original eight keys (quant, gpu,
robotics, perf, ml, systems, hardware, general) is preserved relative to each other, so adding the rest
of the catalog did not silently re-classify anything already scored.

Keys are stable identifiers: `directory/board_directory.csv` tags 740 companies with them and stored
`raw.fast.role` values reference them. Rename a label freely; never a key.

Keywords must start and end alphanumeric (`roletype._alternation` asserts it, since it wraps the whole
alternation in one boundary pair rather than each word). So "c++" and ".net" cannot be keywords — put
those in `fast_scoring.software_keywords`, which uses a different matcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

GENERAL = "general"

#: Weight a newly picked type gets. A shipped catalog must not encode whose taste is right, so every
#: type is worth the same until the user ranks it.
DEFAULT_PICK_WEIGHT = 70


@dataclass(frozen=True)
class RoleType:
    key: str
    label: str
    group: str
    keywords: tuple[str, ...]
    blurb: str = ""
    #: A title hit here settles it, instead of going to the usual count-then-order tie-break. For a type whose
    #: keywords are multi-word phrases carved out of a broader type, counting is the wrong test: "Quantitative
    #: Trading Intern" is one phrase hit for Quant Research but two word hits ("quantitative", "trading") for
    #: HFT / Quant engineering, so the type it was split out of would win every time.
    decisive: bool = False


#: Groups, in the order the picker shows them.
GROUPS: tuple[str, ...] = (
    "Low-level & performance", "Systems & infrastructure", "AI & data",
    "Physical & scientific", "Products & apps", "Other engineering",
)


CATALOG: tuple[RoleType, ...] = (
    # ---- Low-level & performance -------------------------------------------------------------
    #: Listed before `quant` so an explicit research/trading title wins the tie. The two are different jobs —
    #: building the trading system vs. finding the edge — and wanting one without the other is common enough
    #: that a single "HFT / Quant" bucket cannot express it.
    RoleType("quantresearch", "Quant Research / Trading", "Low-level & performance", (
        "quantitative researcher", "quantitative research", "quantitative trader", "quantitative trading",
        "quantitative developer", "quantitative analyst", "quantitative strategist", "quant researcher",
        "quant research", "quant trader", "quant trading", "quant analyst", "alpha research",
        "systematic trading", "portfolio construction", "derivatives pricing", "stochastic"),
        "Finding the edge: alpha research, signals, pricing, systematic strategy.", decisive=True),
    RoleType("quant", "HFT / Quant engineering", "Low-level & performance", (
        "trading", "trader", "quant", "quantitative", "hft", "high frequency", "high-frequency", "market data",
        "market making", "market-making", "order book", "matching engine", "exchange connectivity", "hedge fund",
        "prop trading", "proprietary trading", "kernel bypass", "tick data"),
        "Trading systems, market data, execution. Latency is the product."),
    RoleType("gpu", "GPU / Accelerators", "Low-level & performance", (
        "gpu", "cuda", "tensorrt", "triton", "rocm", "cudnn", "cutlass", "accelerator", "accelerated compute",
        "ai compiler", "ai compilers", "inference", "inference optimization", "kernel optimization", "gpu kernels",
        "tensor core",
        "deep learning computer architecture", "computer architecture", "ai inference", "npu", "tpu",
        "co design", "co-design", "developer technology", "hpc", "high performance computing",
        "high-performance computing"),
        "Kernels, inference optimization, accelerator architecture."),
    RoleType("compilers", "Compilers / Runtimes", "Low-level & performance", (
        "compiler", "llvm", "mlir", "codegen", "code generation", "jit", "intermediate representation",
        "toolchain", "interpreter", "language runtime", "garbage collect", "static analysis", "parser",
        "type system", "linker"),
        "Front ends, optimizers, JITs, language runtimes and toolchains."),
    RoleType("silicon", "Silicon / RTL / EDA", "Low-level & performance", (
        "rtl", "verilog", "systemverilog", "vhdl", "asic", "physical design", "design verification",
        "silicon", "chip design", "eda", "synthesis", "place and route", "dft", "analog design",
        "timing closure", "soc design"),
        "Chip design and verification, EDA tooling."),

    # ---- Physical & scientific (specific, so they win ties over the broad buckets) -------------
    RoleType("aerospace", "Aerospace / Avionics", "Physical & scientific", (
        "aerospace", "avionics", "spacecraft", "satellite", "launch vehicle", "propulsion", "orbital",
        "attitude control", "space systems", "astrodynamics"),
        "Flight and space systems software."),
    RoleType("robotics", "Robotics / Autonomy", "Physical & scientific", (
        "robotics", "robot", "robotic", "autonomy", "autonomous", "self-driving", "robotaxi", "perception",
        "motion planning", "planning & controls", "planning and controls", "controls", "slam", "localization",
        "gnc", "guidance", "navigation", "ros", "ros 2", "ros2", "isaac sim", "drone", "uav", "flight software",
        "manipulation", "humanoid", "hardware in loop", "hardware-in-the-loop", "hardware-in-loop", "hil",
        "sensor fusion", "lidar", "teleoperation"),
        "Planning, control, perception, simulation, real-time autonomy."),

    # ---- AI & data (specific first) ------------------------------------------------------------
    RoleType("cv", "Computer Vision", "AI & data", (
        "computer vision", "image processing", "object detection", "segmentation", "visual perception",
        "3d reconstruction", "photogrammetry", "pose estimation", "optical flow"),
        "Detection, segmentation, geometry from images."),
    RoleType("graphics", "Graphics / Rendering", "Products & apps", (
        "graphics", "rendering", "renderer", "shader", "ray tracing", "raytracing", "rasteriz", "opengl",
        "vulkan", "directx", "metal api", "path tracing", "real-time rendering"),
        "Renderers, shaders, real-time graphics pipelines."),
    RoleType("gamedev", "Game Development", "Products & apps", (
        "game", "gameplay", "unreal", "unity", "game engine", "level design", "game systems"),
        "Engines, gameplay systems, tools."),
    RoleType("ar", "AR / VR / XR", "Products & apps", (
        "augmented reality", "virtual reality", "mixed reality", "spatial computing", "headset", "xr", "ar vr",
        "hand tracking", "eye tracking"),
        "Headsets, spatial computing, tracking."),
    RoleType("compbio", "Computational Biology", "Physical & scientific", (
        "computational biology", "bioinformatics", "genomics", "genomic", "protein", "molecular dynamics",
        "drug discovery", "sequencing", "cheminformatics", "structural biology"),
        "Genomics, molecular simulation, scientific pipelines."),
    RoleType("fintech", "Fintech / Payments", "Other engineering", (
        "payments", "fintech", "banking", "ledger", "settlement", "risk engine", "fraud detection",
        "credit risk", "treasury", "clearing"),
        "Payment rails, ledgers, risk and fraud systems."),
    RoleType("security", "Security / AppSec", "Systems & infrastructure", (
        "security", "appsec", "infosec", "cryptography", "cryptographic", "penetration testing", "red team",
        "vulnerability", "threat detection", "malware", "zero trust", "identity and access", "secure enclave"),
        "Application, infrastructure and product security."),

    # ---- perf keeps its original position, ahead of ml / systems / hardware ---------------------
    RoleType("perf", "Performance / Optimization", "Low-level & performance", (
        "performance", "optimization", "optimisation", "low latency", "low-latency", "latency", "profiling",
        "simd", "avx", "vectoriz", "efficiency", "performance-critical", "high-performance", "throughput",
        "cache-friendly", "cache locality", "microarchitecture", "lock-free", "nanosecond", "hot path",
        "real-time constraints",
        # Parallelism and CPU-level work had no vocabulary anywhere in the catalog: "Parallel Computing Intern"
        # and "CPU Performance Intern" both used to fall through to General. Bounded matching means the -ism and
        # -ed forms have to be listed separately.
        "parallel", "parallelism", "parallel computing", "parallel programming", "concurrent", "multicore",
        "multi-core", "multithreading", "multithreaded", "numa", "openmp", "mpi", "cpu", "instruction set",
        "memory bandwidth", "scalability"),
        "Making things fast at a low level: profiling, hot paths, parallelism, memory and CPU behaviour."),

    RoleType("mlresearch", "ML Research", "AI & data", (
        "research scientist", "research intern", "machine learning research", "ai research", "research engineer",
        "publication", "novel architectures", "foundation model research"),
        "Publication-oriented research roles."),
    RoleType("database", "Databases / Storage", "Systems & infrastructure", (
        "database", "databases", "storage engine", "database internals", "query engine", "query optimizer",
        "storage", "transaction", "indexing", "columnar", "olap", "oltp", "key-value store"),
        "Storage engines, query execution, transactional systems."),
    RoleType("networking", "Networking", "Systems & infrastructure", (
        "networking", "network engineer", "networking stack", "tcp", "rdma", "packet processing", "routing",
        "switching", "load balancer", "cdn", "dpdk", "ebpf", "protocol design"),
        "Protocols, packet paths, network data planes."),
    RoleType("cloud", "Cloud / DevOps / SRE", "Systems & infrastructure", (
        "cloud", "sre", "site reliability", "devops", "kubernetes", "terraform", "observability", "ci cd",
        "platform engineering", "deployment", "provisioning", "incident response"),
        "Running things in production: reliability, deployment, observability."),
    RoleType("data", "Data Engineering", "AI & data", (
        "data engineer", "data engineering", "data infrastructure", "data pipeline", "etl", "spark",
        "data warehouse", "streaming data", "kafka", "airflow", "data lake"),
        "Pipelines and warehouses that move data at scale."),
    RoleType("datasci", "Data Science / Analytics", "AI & data", (
        "data science", "data scientist", "analytics", "business intelligence", "statistician", "experimentation",
        "ab testing", "a/b testing", "causal inference", "forecasting"),
        "Analysis, experimentation, statistical modelling."),
    RoleType("ml", "ML / AI Infra", "AI & data", (
        # "inference" alone belongs to GPU, not here: it is the serving/optimization word far more often than the
        # modelling one, and as a bare token it used to let ML outvote a specific phrase like "ai inference".
        "machine learning", "ml", "ai", "deep learning", "ml infrastructure", "ai infrastructure", "ai platform",
        "ml platform", "mlops", "llm", "training", "applied ml", "recommendation", "generative ai",
        "model serving", "fine-tuning", "rag", "embeddings", "reinforcement learning"),
        "Training and inference infrastructure, applied ML systems."),

    RoleType("devtools", "Developer Tools / Build", "Other engineering", (
        "developer tools", "developer experience", "build system", "bazel", "developer productivity",
        "sdk", "api design", "ide", "tooling engineer", "release engineering"),
        "SDKs, build systems, the tools other engineers use."),
    RoleType("qa", "QA / Test Automation", "Other engineering", (
        "qa engineer", "quality assurance", "test automation", "test engineer", "sdet", "validation engineer",
        "verification and validation", "automated testing"),
        "Test infrastructure and automation."),
    RoleType("mobile", "Mobile", "Products & apps", (
        "ios", "android", "mobile", "swift", "kotlin", "react native", "flutter", "mobile app"),
        "iOS and Android applications."),
    RoleType("frontend", "Frontend / Web", "Products & apps", (
        "frontend", "front-end", "front end", "web developer", "javascript", "typescript", "react", "vue",
        "angular", "css", "web app", "user interface", "design systems"),
        "Browser applications and interfaces."),
    RoleType("fullstack", "Full-stack / Product", "Products & apps", (
        "full stack", "full-stack", "fullstack", "product engineer", "web application", "crud", "saas",
        "rails", "django", "node js"),
        "End-to-end product features."),

    # ---- the broad buckets, last so anything specific outranks them ----------------------------
    RoleType("systems", "Systems / Infra", "Systems & infrastructure", (
        "distributed systems", "distributed system", "infrastructure", "infra", "systems software",
        "system software", "operating system", "kernel development", "kernel", "linux", "backend", "back-end",
        "platform", "large-scale systems", "consensus", "raft", "paxos", "rpc framework", "concurrency",
        "concurrency primitives", "scheduler", "microservices"),
        "Server-side systems where correctness and scale are the product."),
    RoleType("hardware", "Embedded / Hardware", "Physical & scientific", (
        "embedded", "firmware", "fpga", "soc", "bare metal", "bare-metal", "rtos", "microcontroller",
        "board bring-up", "device driver", "hardware", "battery management", "power electronics", "pcb",
        "signal integrity"),
        "Bare-metal and board-level work."),
    RoleType("it", "IT / Support", "Other engineering", (
        "help desk", "helpdesk", "technical support", "it support", "system administrator", "sysadmin",
        "desktop support", "service desk"),
        "Internal IT and end-user support."),
)

#: The fallback: a posting whose title matches nothing at all. Always last, never removable.
GENERAL_TYPE = RoleType(GENERAL, "General SWE", "Other engineering", (),
                        "A software job the catalog could not place more precisely.")

ALL: tuple[RoleType, ...] = CATALOG + (GENERAL_TYPE,)
BY_KEY: dict[str, RoleType] = {t.key: t for t in ALL}
KEYS: tuple[str, ...] = tuple(t.key for t in ALL)
LABELS: dict[str, str] = {t.key: t.label for t in ALL}


@lru_cache(maxsize=1)
def by_group() -> list[tuple[str, list[RoleType]]]:
    """The catalog arranged for the picker. `general` is excluded: it is the fallback, not a choice."""
    out: list[tuple[str, list[RoleType]]] = []
    for g in GROUPS:
        members = [t for t in CATALOG if t.group == g]
        if members:
            out.append((g, members))
    return out


def label(key: str) -> str:
    return LABELS.get(key, key)


#: Keys that existed before the catalog was widened, mapped to the catalog entry a posting that used to
#: classify as them would now land in. Used once, by `config` migration, to carry a user's old
#: `fast_scoring.role_weights` onto the split-out types so widening the catalog changes no score.
SPLIT_FROM: dict[str, str] = {
    "compilers": "gpu",         # "compiler"/"llvm"/"mlir" used to be GPU keywords
    "silicon": "hardware",      # rtl/asic/verilog used to be Embedded / Hardware
    "cv": "ml",                 # "computer vision" used to be an ML keyword
    "database": "systems", "networking": "systems", "cloud": "systems", "data": "systems",
}
