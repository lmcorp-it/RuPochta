# RuPochta Project Next Steps

**Baseline:** `main @ b8782d99819540c2ed840a96cbea5d9797b9ceae`  
**Decision:** security stabilization before feature expansion.  
**Sequence:** Guide → Secure → Verify → Standardize → Decompose → Expand.

## Release blockers

1. **P0 — LDAPS certificate validation**
   - Require validated TLS for every LDAP bind.
   - Pass an explicit `ldap3.Tls(validate=ssl.CERT_REQUIRED, ...)` object.
   - Reject plaintext LDAP in production.
   - Test trusted CA, self-signed, wrong-hostname and expired certificates.

2. **P1 — DNS-rebinding-safe custom IMAP/SMTP egress**
   - Re-resolve and validate immediately before every connection.
   - Pin the validated IP for TCP while preserving hostname for TLS SNI/certificate verification.
   - Revalidate historical custom bindings.
   - Add a deny-by-default egress policy for private/loopback/link-local/metadata networks.

3. **P1 — Terminal credential cleanup**
   - Clear secrets in the same transaction that sets `sent`, `cancelled`, permanent `failed`, `woken` or abandoned states.
   - Create immediate failed-send records without a reusable password.
   - Migrate existing terminal rows.

4. **P1 — Complete key rotation**
   - Separate subject hashing from encryption with `WEBMAIL_SUBJECT_HASH_KEY`.
   - Support current/previous hash keys during migration.
   - Migrate secrets in SSO bindings, external bindings and ticket intake tables.
   - Add a dry-run and rollback-safe rotation command.

5. **P1 — Secretless MCP authentication**
   - Remove mailbox password from the model-facing `rupochta_login` schema.
   - Use environment/session-cookie/keychain/OAuth or an out-of-band host prompt.
   - Explicit logout discards credentials by default.

## Follow-up hardening

- Make MCP read-only reads state-preserving (`mark_seen=false` by default).
- Set `url_relative="deny"` in the HTML sanitizer and test protocol-relative tracking pixels.
- Render PDF/text attachment previews from fetched Blob URLs so global `X-Frame-Options: DENY` can remain enforced.
- Move CSP from report-only to enforced after inline scripts are removed or nonce/hash protected.

## Engineering foundation

- Add `pyproject.toml`, Ruff, a type checker and a committed Python lockfile.
- Add root `.env.example`, CODEOWNERS, Dependabot/Renovate and pre-commit.
- Pin GitHub Actions by SHA.
- Add `pip-audit`, npm audit, SBOM and container scanning.
- Replace source-string tests with real ASGI integration tests through an application factory and injected protocol adapters.

## AI / Codex readiness

- [x] Add `AGENTS.md` with repository map, commands, security invariants and PR rules in the documentation-guidance PR.
- [x] Add `.github/copilot-instructions.md` that points to `AGENTS.md` in the documentation-guidance PR.
- [ ] Add `docs/architecture.md` and a root `Makefile` or `justfile`.
- [ ] Add a sanitized `.vscode/mcp.json.example` with read-only defaults and placeholders only.
- [ ] Add focused security-review and release-check agents/skills after ownership is assigned.

## Product roadmap after the gates

1. Implement issue #16: repeatable priority and owner mapping.
2. Complete issue #6: PWA installation/offline/notification matrix across platforms.
3. Stabilize the Tauri desktop shell and release contract.
4. Take Linux packaging (#11) first, then macOS signing/notarization (#10).
5. Build the localization foundation and English UI (#4).
6. Start Android (#12), then iOS (#13), after API/offline/push contracts are stable.

## Required PR order

0. `docs/project-execution-guidance` — establish the operating rules and release checklist without changing runtime behavior.
1. `security/ldaps-certificate-validation`
2. `security/custom-endpoint-safe-dialer`
3. `security/terminal-secret-cleanup`
4. `security/key-rotation-v2`
5. `mcp/secretless-auth`
6. `mcp/true-readonly`
7. `frontend/mail-preview-hardening`
8. `ci/reproducible-python`
9. `test/asgi-security-harness`
10. `refactor/app-factory-and-services`

## Release gate

Do not remove the release block until P0/P1 acceptance criteria are proven with fresh command output and required CI checks are green on the release commit.
