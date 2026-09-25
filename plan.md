# Project Brief: Autonomous Internship Search & Evaluation Engine

## 1. Project Objective
I want to build a local, Claude-powered engine to automate my internship hunt. The system should autonomously search for, scrape, evaluate, and track internship postings based on my specific career profile and preferences. 

**Note to Claude:** This document outlines my initial thoughts. Nothing is set in stone. Before we write any code, please review this architecture, point out any flaws, and recommend better tools, MCPs, or workflows if they exist.

## 2. Proposed Architecture & Toolchain
Here is the stack I am currently considering. I plan to use **Claude Code** (or Claude Desktop) as the orchestrator.

*   **Web Scraping & Discovery:** Firecrawl MCP (to bypass bot protection, render JS, and convert job boards into Markdown).
*   **Source Integrations:** GitHub MCP (to read popular internship aggregation repositories like PittCSC or SimplifyJobs).
*   **Memory & Tracking:** SQLite MCP (to store job URLs, application statuses, and prevent re-evaluating the same jobs).
*   **Evaluation Engine:** You (Claude), utilizing a structured scoring rubric against a local `profile.md` file.

*Question for Claude: Are there better MCP servers or open-source scaffolding (e.g., Servation/job-search-mcp) we should fork or integrate instead of building from scratch?*

## 3. Core Workflows to Build
I want the system to handle the following loop:

1.  **Ingestion:** Scrape target URLs (GitHub repos, niche job boards, ATS sites like Greenhouse/Lever). 
2.  **Deduplication:** Check the SQLite database to see if a job URL has already been processed.
3.  **Evaluation & Scoring:** Read the job description and compare it to my `profile.md`. Generate a structured JSON/Markdown output that includes:
    *   A fit score (0-100).
    *   Term check (e.g., Is it strictly a Summer term? Is the length appropriate?).
    *   Red flags (e.g., requires citizenship I don't have, unpaid, outside target locations).
    *   Summary reasoning.
4.  **Logging:** Save the evaluation back to SQLite.
5.  **Actionable Output:** Present a daily digest of jobs scoring >80 for me to manually review or apply to.

## 4. User Profile & Constraints (To be expanded in `profile.md`)
*We will define the specifics later, but the engine must be able to strictly filter based on:*
*   **Target Roles:** [Insert Roles, e.g., SWE, Data Science, UI/UX, Hardware]
*   **Work Term & Length:** [Insert Term, e.g., Summer 2027, 12-16 weeks]
*   **Location Preferences:** [Insert Locations, e.g., Vancouver, Remote, US-eligible]
*   **Dealbreakers:** [Insert Dealbreakers, e.g., Unpaid, clearance required]

## 5. Next Steps for Claude
To kick this off, please do the following:
1.  **Critique this plan:** What are the biggest technical hurdles (e.g., LinkedIn scraping limits) and how should we work around them?
2.  **Recommend a data schema:** Draft a lightweight SQLite schema for tracking the jobs.
3.  **Propose a step-by-step roadmap:** How should we sequence the build? (e.g., Step 1: Set up SQLite MCP, Step 2: Write the scoring prompt, etc.)

I am ready for your feedback. Let's build this.