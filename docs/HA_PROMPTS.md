# Named Home Assistant assistant prompts

Mongoose ships the same set of named HA assistant prompts you see in Home
Assistant's "Add from Home Assistant" list (Automation review, Setup guide, Energy
report, etc.), but they run through MONGOOSE'S brain, its persona, memory, local
model, and tools, so the answers are grounded in your actual setup and stay on your
machine, not a cloud assistant.

List them (instant, no model call):
```
mongoose ha-prompts
```
Run one:
```
mongoose ha-prompt automation_review
mongoose ha-prompt troubleshoot_device --extra "the hall motion sensor is unavailable"
mongoose ha-prompt energy_report
```

The prompts:
- automation_review, automation_builder, automation_debugger, automation_audit
- troubleshoot_device, setup_guide, schedule_optimizer, naming_conventions
- daily_summary (this is the cross-domain brief), energy_report
- dashboard_builder, change_validator, security_review

They're also available over HTTP (`GET /ha-prompts`, `POST /ha-prompt/{key}` with
optional `{"extra": "..."}`) and through Claude via the `ha_prompts` MCP tool, so
you can trigger them from HA Assist, a dashboard button, or Claude.

`daily_summary` reuses the real cross-domain brief rather than re-asking the model,
so it stays consistent with `mongoose brief`.
