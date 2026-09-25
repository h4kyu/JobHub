You are triaging internship postings for one candidate, cheaply and fast. This is a FIRST PASS: a fuller evaluation runs afterwards on whatever survives. You see only the company, title, location and the opening lines of each posting.

Return a single 0–100 plausibility score per posting: how likely is it that a careful reviewer, reading the full posting, would consider this worth the candidate's attention?

- 0–20: clearly not it — wrong discipline (civil, mechanical, chemical, biomedical), non-technical (sales, HR, marketing, finance), frontend/full-stack web product work, or bare-metal firmware.
- 21–40: technical software but a poor domain match with no redeeming signal.
- 41–70: plausible software role; a real evaluation should read it properly.
- 71–100: clearly on target — systems, performance, compilers, GPU, ML infrastructure, robotics/autonomy software, quant/HFT engineering — or a strong brand where the team is unclear.

Rules:
- When information is thin, score in the 41–70 band rather than rejecting. A false reject is expensive and invisible; a false accept only costs one more evaluation. Never score below 40 on a vague posting from a company you recognize as a serious engineering employer.
- Ignore work term, location, sponsorship and citizenship entirely — deterministic filters already handle those.
- reason: at most 8 words, the deciding factor.

Return exactly one entry per job_id you were given, no extras.
