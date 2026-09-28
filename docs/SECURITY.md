# Security model

The working Mac profile is read-only by construction, not just by prompt.

- Repository access must resolve under an allowed root.
- Common credential directories are denied.
- `code_read` rejects path escapes and files outside the repository.
- Search scopes are resolved and confined before `rg` runs.
- Git operations are fixed read-only argument lists; the model never supplies a raw Git command.
- No shell tool is exposed through MCP.
- No repository writes, commits, pushes, subprocess execution requested by the model, or child agents exist.
- Provider endpoints must use HTTP and an allowlisted host. The default is loopback-only.
- No cloud fallback exists inside Gremlins.
- Metrics contain counts/statuses rather than prompts, source contents or logs.

External executable plugins and write-capable workers are deliberately not implemented yet. Adding either requires a real execution sandbox and a separate permission model; an instruction saying "read-only" is not sufficient.
