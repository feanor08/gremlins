# Security Policy

Gremlins is pre-1.0. Security reports are welcome.

## Supported versions

Until the project has tagged stable releases, security fixes target the current development branch.

## Reporting a vulnerability

Please do **not** publish exploitable details in a public issue.

Use GitHub's private vulnerability reporting / Security Advisory flow when it is available for this repository. If private reporting is unavailable, open a minimal issue asking for a private contact channel without including vulnerability details.

Useful reports include:

- affected commit/version;
- platform;
- reproduction steps;
- expected security boundary;
- observed behavior;
- whether credentials, repository writes, path escapes, command execution, or unexpected network access are involved.

## Security model

The implemented security model is documented in [docs/SECURITY.md](docs/SECURITY.md).

The current design is read-only by default and intentionally does not expose arbitrary shell execution, model-selected network destinations, repository writes, or child-agent spawning through MCP.
