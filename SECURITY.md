# Security Policy

## Supported Versions

This project moves quickly and currently supports the latest `main` branch.
Please verify reported issues against current `main` before filing them.

## Reporting a Vulnerability

Do not open a public GitHub issue for a sensitive security report.

Preferred reporting path:

1. Use GitHub private vulnerability reporting for this repository if it is available.
2. If that is not available, contact the maintainer directly through GitHub and include:
   - affected `acw` version or commit
   - exact reproduction steps
   - tmux / Codex environment details
   - impact and any known mitigations

Please avoid posting proof-of-concept details publicly until the issue has been confirmed and a fix or mitigation is available.

## Scope

The most relevant classes of reports for this project are:

- unintended command injection through pane messages or shell handling
- unsafe tmux target resolution or cross-session interference
- disclosure of Codex state, logs, or auth material under `~/.codex`
- unsafe cleanup or process-management behavior that can affect unrelated sessions
