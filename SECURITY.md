# Security Policy

Film Club Tracker is self-hosted software that stores credentials for
third-party services (TMDB, and optionally Plex and Seerr) and per-user Plex
tokens. We take security seriously and appreciate responsible disclosure.

## Supported versions

The project is pre-1.0. Security fixes are applied to the latest release only.
Once `1.0.0` ships, this section will list supported version ranges.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **Report a vulnerability** flow
(repository → *Security* → *Advisories* → *Report a vulnerability*), which
creates a private advisory visible only to the maintainers.

Please include:

- a description of the issue and its impact,
- steps to reproduce (a minimal proof of concept if possible),
- affected version/commit, and
- any suggested remediation.

We aim to acknowledge reports within a few days and to coordinate a fix and
disclosure timeline with you.

## Scope and expectations

In scope:

- Authentication and session handling (local accounts, Plex login, sessions).
- Secret storage and the settings/configuration surface.
- Injection, access-control, CSRF, and SSRF issues in the API or SPA.
- Container hardening and the deployment tooling.

Out of scope:

- Vulnerabilities that require an already-compromised host or physical access.
- Denial of service from an authenticated, trusted club member.
- Issues in third-party services (Plex, TMDB, Seerr) themselves.

## Handling of secrets

- Do **not** include real API keys, tokens, passwords, session or webhook
  secrets, or private hostnames in reports, issues, pull requests, logs, or
  test fixtures. Use placeholders.
- The application never returns stored secret values to the browser and must
  never log them. If you find a path that does, that is itself a reportable
  issue.

## Deployment guidance

- Serve the application over HTTPS behind a trusted reverse proxy or tunnel.
- Do not expose a plain-HTTP instance to the public internet.
- Keep the `/data` volume (which holds the database and, in future, the master
  key) backed up and access-controlled.
