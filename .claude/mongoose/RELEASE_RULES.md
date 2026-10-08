# Mongoose release rules for Claude Code

## Operating standard

- Treat Alex as the real user: tired, busy, non-technical in the moment, and furious if the installer lies.
- Do not claim a fix is complete because the architecture looks right. Claim completion only after deterministic checks pass.
- Do not hide terminal output. If an install, npm, pip, test, or browser handoff fails, surface the real error.
- Do not create prettier dead ends. A loading, installing, waiting, or handoff page must have a real timeout, error state, and recovery action.
- Every browser-facing HTML/JS change must have a parse/smoke gate. Escaping bugs are release blockers.
- Every installer/update/remote-access change must be tested as a user journey, not just as isolated functions.
- When working on Cloudflare/DDNS/guest access, verify the public route readiness, not merely local config.
- Never assume Safari will behave like a forgiving dev browser. Avoid fragile inline JavaScript and unescaped template/string literals.
- Never tell Alex to run a command that silently discards the useful error output.
- Prefer surgical patches with tests that reproduce the failure first.

## Release blocker definition

A change is blocked if any of these can happen:

- the browser shows a spinner or “installing” state indefinitely;
- the terminal is doing something but the browser gives no live or bounded explanation;
- the browser says ready while the actual app server is not ready;
- stale state from a previous install can be mistaken for the current run;
- Safari certificate or localhost handoff instructions are missing at the point of need;
- a guest/phone flow can save or open the wrong local-only URL;
- tests pass but the exact first-run path was not exercised.

## Required proof before release-ready wording

Before saying “fixed”, “ready”, “releaseable”, or “ship it”, run:

```bash
scripts/claude_mongoose_gate.sh
```

If the gate cannot be run, say exactly why and give the unrun risk. Do not substitute optimism.
