#!/usr/bin/env python3
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if payload.get("tool_name") != "Bash":
    sys.exit(0)

cmd = str(payload.get("tool_input", {}).get("command", ""))
cmd_flat = " ".join(cmd.split())

blocks = [
    (r"\brm\s+-rf\s+/(\s|$)", "Refusing rm -rf /."),
    (r"\bsudo\s+rm\s+-rf\b", "Refusing sudo rm -rf."),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "Refusing chmod -R 777 /."),
    (r"\bsudo\s+npm\s+install\s+-g\s+@anthropic-ai/claude-code\b", "Do not install Claude Code with sudo. Fix npm prefix instead."),
    (r"\bANTHROPIC_API_KEY=[^\s]+\s+claude\b", "Do not run Claude Code with ANTHROPIC_API_KEY inline; this risks API-rate billing."),
]

for pattern, reason in blocks:
    if re.search(pattern, cmd_flat):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
        sys.exit(0)

sys.exit(0)
