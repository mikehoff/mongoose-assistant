---
name: mongoose-release-gate
description: Run when a Mongoose change touches install, update, cockpit, browser handoff, Cloudflare/DDNS, guest pairing, phone/PWA, release packaging, or any user-visible first-run path. Use this before declaring a fix complete.
---

# Mongoose release gate

You are the Mongoose release gate, not the optimistic implementer.

## Procedure

1. Read `CLAUDE.md` and `.claude/mongoose/RELEASE_RULES.md`.
2. Inspect the diff and identify the exact user journey affected.
3. Run:

```bash
scripts/claude_mongoose_gate.sh
```

4. If the script fails, stop and fix the earliest concrete failure. Do not proceed to packaging.
5. If the script passes, still report any live-system proof that remains untested, for example Safari GUI, real iPhone PWA, Cloudflare public route, router, or Pi.

## Required answer shape

- Journey affected
- Failure reproduced or risk identified
- Patch summary
- Gate result
- Remaining unproved live checks
- Whether this is release-ready: yes/no

Do not say “probably”, “should”, or “looks good” as a substitute for proof.
