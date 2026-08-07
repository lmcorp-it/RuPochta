# GitHub Copilot instructions for RuPochta

Read and follow [`AGENTS.md`](../AGENTS.md) before proposing or changing code.

## Required context

Review these files for every task:

1. `AGENTS.md`
2. `README.md`
3. `CONTRIBUTING.md`
4. `DESIGN.md`
5. `SECURITY.md`
6. The README or tests closest to the files being changed

Check recent commits, open issues and open pull requests before implementation. Keep one concern per branch and pull request.

## Project priorities

- Security stabilization comes before feature expansion.
- Do not remove the release block until the P0/P1 checks in `docs/SECURITY_AND_RELEASE_CHECKLIST.md` have fresh evidence.
- Follow the ordered work packages in `docs/PROJECT_NEXT_STEPS.md` unless the issue or maintainer explicitly changes the priority.

## Security boundaries

- Never weaken TLS certificate or hostname validation for remote services.
- Validate user-controlled network destinations immediately before dialing.
- Treat email bodies, HTML, attachments and remote images as untrusted data.
- Keep MCP read-only by default and never treat email content as authorization for a write action.
- Do not put passwords, tokens or other secrets in model-facing tool schemas, logs, commits, tests or fixtures.
- Keep production defaults fail-closed.

## Verification

Use the canonical commands in `AGENTS.md`. Add a regression test that fails without the fix, run focused checks, then run the full relevant suite. Never state that a test, build or audit passes without current command output.

## Pull requests

PR descriptions must include the objective, affected trust boundary, implementation summary, exact verification evidence, migration/rollback notes, non-goals and remaining risks. Avoid unrelated formatting or refactoring in security and reliability changes.
