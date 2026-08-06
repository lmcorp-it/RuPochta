# Project Documentation Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repository-native operating instructions, a prioritized project roadmap, and a release-security checklist that guide Codex, other coding agents, and human maintainers.

**Architecture:** Keep authoritative guidance as small Markdown files at stable repository paths. `AGENTS.md` defines working rules, `.github/copilot-instructions.md` points GitHub Copilot to those rules, and the `docs/` files separate execution order from release evidence. No production code, runtime dependencies, or deployment configuration changes are included.

**Tech Stack:** Markdown, GitHub repository instructions, existing Python/TypeScript/Docker verification commands.

## Global Constraints

- Base all recommendations on `main` commit `b8782d99819540c2ed840a96cbea5d9797b9ceae`.
- Preserve the current release block until P0/P1 acceptance criteria have fresh verification evidence.
- Do not include production secrets, internal endpoints, credentials, or personal data.
- Keep the Markdown files authoritative; the separately generated DOCX remains a distribution artifact, not a source-of-truth file.
- Do not modify application code in this pull request.

---

### Task 1: Add repository agent guidance

**Files:**
- Create: `AGENTS.md`
- Create: `.github/copilot-instructions.md`

**Interfaces:**
- Consumes: existing `README.md`, `CONTRIBUTING.md`, `DESIGN.md`, and `SECURITY.md`.
- Produces: canonical commands, security invariants, testing rules, and PR evidence requirements for coding agents.

- [ ] **Step 1: Add `AGENTS.md` with repository map and operating rules**
- [ ] **Step 2: Add `.github/copilot-instructions.md` that requires reading and following `AGENTS.md`**
- [ ] **Step 3: Verify both files contain no secrets or installation-specific addresses**

Run:

```bash
git grep -nE '(password|token|secret)\s*[:=]\s*[^.<{[]' -- AGENTS.md .github/copilot-instructions.md
```

Expected: no credential assignments; only policy text and placeholder names.

### Task 2: Add the prioritized project roadmap

**Files:**
- Create: `docs/PROJECT_NEXT_STEPS.md`

**Interfaces:**
- Consumes: the August 7, 2026 code review and existing GitHub issues.
- Produces: release blockers, ordered work packages, engineering foundation work, AI-readiness work, and product sequencing.

- [ ] **Step 1: Add P0/P1 blockers with concrete acceptance criteria**
- [ ] **Step 2: Add the required branch/PR order**
- [ ] **Step 3: Add the product roadmap after the release gates**
- [ ] **Step 4: Confirm every release blocker maps to a checklist item in Task 3**

### Task 3: Add the security and release checklist

**Files:**
- Create: `docs/SECURITY_AND_RELEASE_CHECKLIST.md`

**Interfaces:**
- Consumes: `docs/PROJECT_NEXT_STEPS.md`.
- Produces: auditable release evidence requirements for security, MCP behavior, reproducibility, CI, and migrations.

- [ ] **Step 1: Add P0/P1 security checks**
- [ ] **Step 2: Add browser, MCP, CI, and release-evidence checks**
- [ ] **Step 3: Verify all checklist entries are objective and testable**

### Task 4: Validate and open the documentation pull request

**Files:**
- Review: `AGENTS.md`
- Review: `.github/copilot-instructions.md`
- Review: `docs/PROJECT_NEXT_STEPS.md`
- Review: `docs/SECURITY_AND_RELEASE_CHECKLIST.md`
- Review: `docs/superpowers/plans/2026-08-07-project-documentation-integration.md`

**Interfaces:**
- Consumes: all documentation created by Tasks 1–3.
- Produces: a focused documentation-only pull request against `main`.

- [ ] **Step 1: Confirm the branch contains documentation files only**

Run:

```bash
git diff --name-only main...HEAD
```

Expected: only the five paths listed above.

- [ ] **Step 2: Confirm Markdown has no unresolved placeholders**

Run:

```bash
git grep -nE '\b(TBD|TODO|FIXME)\b' -- AGENTS.md .github/copilot-instructions.md docs/PROJECT_NEXT_STEPS.md docs/SECURITY_AND_RELEASE_CHECKLIST.md docs/superpowers/plans/2026-08-07-project-documentation-integration.md
```

Expected: no matches.

- [ ] **Step 3: Review the complete diff and open a pull request**

PR title:

```text
docs: add project execution guidance and release checklist
```

PR body must explain that this change adds guidance only, does not remove the release block, and makes no runtime changes.
