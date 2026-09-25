# Profile

## Background
Computer Engineering at the University of Waterloo (Sept 2024 – May 2029), currently in 2B with three co-op terms done. Citizenship: Canadian and Israeli; also Japanese (passport may need renewal). No US work authorization — US roles need J-1/TN-style intern sponsorship or must be open to international students. Employer evaluations: EXCELLENT (MTO), EXCELLENT (Vireo), Voxelis pending. My work has been embedded systems, robotics/simulation tooling, and C++/Python pipelines — not web or product SWE.

## Experience

### Voxelis AI — Software Engineer / Embedded Systems Intern (May–Aug 2026, Vancouver)
Airborne perception startup (helicopter-mounted camera tracking aircraft).
- Owned HeliSDG, a synthetic-data pipeline: C++17/Eigen trajectory planner (voxel A* over a clearance-carved heightfield, Dubins-style smoothing under bank-rate/vertical-speed limits, camera-relative target anchors, time-layered DAG planner) feeding Isaac Sim 5.1 + Cesium photoreal renders with per-frame masks and bounding boxes. All planning in C++; Python only drives the sim.
- Benchmarked Cesium's rendered terrain against Copernicus GLO-30 and USGS 3DEP DEMs over nine 2 km² patches (RMSE 0.7–5 m flat, 10–26 m steep), justifying planning against a DEM outside the simulator.
- Replaced chartered data-collection flights with a step-locked Isaac Sim + Cesium software-in-the-loop rig; fixed the real bottleneck (tile streaming, not GPU) by ordering renders per terrain patch.
- Built a C++17 telemetry-driven condition engine for autonomous camera capture and a three.js scenario visualizer.

### Vireo Interfaces — VR Peripherals Engineer (Sept–Dec 2025, Kitchener)
Small hardware startup building a VR touch/hover peripheral.
- Proved monocular hover tracking: fine-tuned MobileNetV2 regressing fingertip (x, y) from a single oblique 192×96 camera, ~4 mm MAE on a 7 cm pad, TFLite on a Raspberry Pi 4, no calibration step.
- Self-labelling Python/ROS capture pipeline that interpolates hover position between capacitive taps with a sigmoid speed profile validated against top-view video: 3,000+ labelled frames/min, multi-week collection cut to a day. Reworked the loop (threaded serial reader, ring buffer, batched JPEG) to hold 60 fps.
- Bare-metal CMSIS firmware (USB-CDC, I2C, EXTI) for a stylus sensor module under tight Flash/SRAM limits.

### Waterloo Rocketry — Embedded Firmware Developer (Apr 2025–Sept 2026)
- Repurposed PIC18 flight boards for health checks, ADC acquisition, and CAN telemetry; diagnosed pinout faults with an oscilloscope to avoid a board respin.

### Ontario Ministry of Transportation — Junior Technical Analyst (Jan–Apr 2025, Toronto)
- First co-op, IT operations/support in a large public-sector org. Not engineering-heavy.

### Open source and projects
- **GenomeKit (Deep Genomics)** — three merged PRs to a C++20/Python genomics library: fixed a GFF3 parser bug mislabelling 5′/3′ UTRs on minus-strand transcripts; built the CI pipeline producing manylinux wheels for Python 3.9–3.12 on x86_64 and aarch64.
- **nrf-rc-link** — bidirectional RC protocol over nRF24L01+ (framed packets, sequencing, auto-ACK, CRC-8, aircraft-side link-loss failsafe) on STM32 F1/F4/F7, ~1 km range, for an FPV plane with a custom flight-controller PCB. In progress.
- Written-up physics/EE experiments: dipole antenna directivity vs. length (NanoVNA), RLC damping verification, JPEG/DCT quantization sweep scored by SSIM, Nylon 6/10 synthesis and annealing tensile study.

### Academics
Term averages 85.3 / 89.5 / 88.1, Excellent Standing throughout. Strongest: discrete math (98), materials chemistry (97), numerical methods (93), digital circuits (91), digital computers (90); algorithms & data structures 82. Currently taking Embedded Microprocessor Systems, Systems Programming & Concurrency, Signals & Systems, Probability & Statistics. No formal ML, CV, OS, networking, or distributed-systems coursework yet; ML/CV is self-taught and project-driven.

## Skills
- **C++ (strong, primary):** C++17/20 daily — Eigen, CMake, performance-aware code, parsers, planners; comfortable in large existing codebases and open-source workflows.
- **Python (strong):** pipelines, ROS nodes, NumPy, rasterio/pyproj, TensorFlow/Keras + TFLite export, CI scripting.
- **C / embedded (strong):** bare-metal CMSIS on STM32, PIC18, Arduino; USB-CDC, I2C, SPI radios, EXTI, ADC, CAN; scope-level debugging; tight Flash/SRAM budgets.
- **Robotics & simulation (solid):** ROS/ROS 2, Isaac Sim 5.1, Omniverse Replicator, Cesium, synthetic data generation, DEM/heightfield terrain, path planning (A*, DAG/Dijkstra, smoothing), camera geometry.
- **ML / CV (applied):** fine-tuning CNNs for regression, dataset construction and self-labelling, edge deployment. Not a research background.
- **Tooling (solid):** Linux, Git/GitHub Actions, Docker, CMake, manylinux packaging, Protocol Buffers, MQTT, AWS basics.
- **Hardware/EE (working):** circuits coursework, PCB collaboration, RF basics.
- **Gaps:** no production web/backend/frontend, SQL at scale, or cloud beyond basics; no Go/Java; no CUDA. No Rust yet, but it's the deliberate next language (close to C++) — treat Rust roles as in-stack with a ramp-up note.

## Desired work
The through-line is optimization and efficiency: I want to own a narrow, hard technical problem and go all the way down — not coordinate many tools at a surface level. Directions, all genuinely welcome:

**1. Optimization and performance software.** Making things fast and correct at a low level: C++ hot paths, memory and cache behaviour, profiling-driven work, numerical code, real-time and resource-constrained systems. The nitty-gritty — "10× faster, fit this budget, never miss a deadline" — is what I'm drawn to.
- Quant/HFT roles built on deep C++ are appealing (compensation, prestige, calibre of engineers) and I'd apply. Caveat: pure quant may be a stretch right now, and the domain interests me less than the engineering. Treat deep C++ systems/performance work as the target and quant as one attractive instance of it.
- GPU/accelerator work — kernels, inference optimization, local and edge non-LLM models — is very interesting but I have no direct GPU experience. Score it as a growth direction with a real skills gap.

**2. Systems and infrastructure software.** Server-side systems where performance and correctness are the product: distributed systems, databases and storage engines, networking, compilers and runtimes, cloud/observability infrastructure. I have no production distributed-systems experience — score the skills gap honestly — but this is deliberate target territory, not "generic backend" to penalize.

**3. ML/AI engineering and infrastructure.** Inference and training infrastructure, model/runtime optimization, data and evaluation pipelines, applied ML systems. My ML is applied and project-driven (CNN fine-tuning, TFLite edge deployment, synthetic-data generation at Voxelis). Treat ML infrastructure and applied-ML engineering as real targets; ML *research* expecting publications stays out of scope.

**4. Robotics and autonomy — the software side.** Interesting physical problems, and edge inference is where the field is heading. My experience is real here (C++ planning, Isaac Sim pipelines, perception data, a CV model on a Pi). I want to own a core component — planning, control, perception, simulation fidelity, real-time software — not wire ROS packages, CI, and vendor SDKs together. Applied engineering, not research.

**Firmware: no longer.** Despite the embedded résumé, I'm moving away from firmware. A role whose core job is bare-metal MCU work — SPI/I2C/UART/CAN buses, RTOS, bootloaders, register-level bring-up — should score very low on work_alignment (≤20) no matter how well my skills match. The embedded background is evidence I can work close to the metal, not a direction to match jobs against. (Robotics/autonomy software that merely touches CAN or sensor buses is fine.)

**Languages.** C++ and Python are home. Rust is explicitly in-scope — it's close to C++ and I'll ramp up before the term starts; don't penalize Rust-first systems roles for the language. Frontend JavaScript/TypeScript work is out of scope entirely.

**Reward:** performance, real-time, or resource-constrained requirements; C++- or Rust-first codebases; systems with a clear "correct and fast" definition; an intern owning a component; hardware in the loop (a plus, not required).
**Avoid (score work_alignment low):** frontend or full-stack product work — anything whose deliverable is UI/JavaScript; classic CRUD product backend with no systems depth; bare-metal firmware (above); mostly integration/glue work; data-labelling or ops; research roles expecting publications. Stacks entirely outside C++/C/Python/Rust are a mild penalty only — judge the work first.

## What a great internship looks like
- One or two meaty problems for the term that ship or get used, near senior engineers who are strong in exactly that thing (performance, controls, embedded, GPU).
- **Brand matters in its own right** — for what it signals, the doors it opens, and the people there. Don't push big or prestigious companies down on the assumption their work is shallower; name and depth conflicting is a genuine trade-off, not a rule either way.
- Hardware or physics in the loop is a plus, not a requirement.
- "Research intern" postings only if the work is clearly building systems rather than publishing.
- **Location: US preferred** (better-paying, more impactful companies and startups); otherwise open to anywhere. Location is a mild factor, never a reason to reject.

## Things I'm open to being surprised by
Flag as wildcards roles in fields I think are long-term good bets, even if not an obvious match, as long as there's real engineering (ideally C++/embedded/performance/simulation) underneath:
- **Biology and human enhancement:** longevity, gene editing, embryo selection / reproductive genetics, performance enhancement and pharmacology, computational-biology infrastructure (I've contributed to a genomics C++ library).
- **Society-scale systems:** prediction markets, large-scale simulation of people and markets, surveillance and security technology, charter / privately organized cities and their tooling, AI companions.
- **Energy and physics:** fusion, photonics, weather modification / geoengineering.
- **Defence and autonomous systems:** autonomous warfare, drones, targeting and tracking — only if open to Canadians (many require citizenship or clearance).
- Any small, intense team building physical or high-performance systems in an unglamorous but important niche.

Not wildcards: buzzword-domain startups doing ordinary CRUD engineering, crypto/web3, ad-tech, or sales/ops roles with an engineering title.
