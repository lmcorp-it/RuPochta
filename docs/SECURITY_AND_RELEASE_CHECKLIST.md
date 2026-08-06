# RuPochta Security and Release Checklist

## P0/P1 release blockers

- [ ] LDAPS validates certificate chain, hostname and expiry.
- [ ] Plain LDAP is rejected in production or upgraded with mandatory verified STARTTLS.
- [ ] Custom IMAP/SMTP targets are validated immediately before dialing.
- [ ] DNS rebinding regression tests pass.
- [ ] Egress policy blocks internal, loopback, link-local and metadata networks.
- [ ] All terminal queue and worker rows have empty secret fields.
- [ ] Historical terminal rows are cleaned by migration.
- [ ] Key rotation works for SSO bindings, external bindings, ticket intake, shared mailboxes and sessions.
- [ ] A previous key can be retired after migration.
- [ ] MCP tool schemas contain no mailbox-password argument.
- [ ] Explicit logout does not silently sign back in.

## Web and browser security

- [ ] Email HTML is sanitized with an allowlist parser.
- [ ] Relative and protocol-relative remote image URLs are denied.
- [ ] Remote resources do not load before user consent.
- [ ] Message and attachment content is sandboxed.
- [ ] PDF/text preview works without weakening `X-Frame-Options` or CSP.
- [ ] CSP is enforced, or the remaining report-only exception is documented and approved.
- [ ] Cookies, CSRF/origin checks and trusted-proxy behavior have real HTTP tests.

## MCP and AI safety

- [ ] Read-only tools are state-preserving.
- [ ] Write tools are disabled by default.
- [ ] Email/attachment prompt-injection regression tests exist.
- [ ] Sending and permanent deletion have explicit confirmation boundaries.
- [ ] AGENTS.md and Copilot instructions are present and consistent.
- [ ] MCP configuration examples contain placeholders only.

## Reproducibility and CI

- [ ] Python dependencies are locked.
- [ ] Node dependencies use the committed lockfile and `npm ci`.
- [ ] Ruff/formatter and type checks are required.
- [ ] Python and Node dependency audits are required.
- [ ] SBOM and container scan artifacts are produced.
- [ ] GitHub Actions are pinned by commit SHA.
- [ ] CODEOWNERS and dependency update automation are configured.
- [ ] A clean clone can build and test using documented commands.

## Release evidence

- [ ] All required checks are green on the exact release commit.
- [ ] Migration and rollback were tested with production-like fixtures.
- [ ] Key-rotation and backup-restore drills are documented.
- [ ] PWA/platform claims in README match tested behavior.
- [ ] No secrets, internal endpoints or personal data are present in the release diff.
- [ ] Remaining limitations are documented in release notes.
