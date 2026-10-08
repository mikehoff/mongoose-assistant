"""Interactive DDNS/remote-reach setup: choose a provider, enter its details, record the choice.

Recommends Tailscale (no port-forward, no public IP exposure) but lets you pick any of the six paths.
For the DDNS providers it collects only the keys that provider needs and states the port-forward +
public-IP trade-offs plainly, no pretending a hostname alone makes Mongoose reachable.
"""
from __future__ import annotations


def _prompt(label: str, secret: bool = False) -> str:
    try:
        if secret:
            import getpass
            return getpass.getpass(f"{label}: ").strip()
        return input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def setup_ddns(provider: str | None = None, yes: bool = False) -> int:
    """Choose and configure a remote-reach provider. Returns an exit code."""
    from .ddns import PROVIDERS, provider_by_slug, setup_tailscale, update_now
    from .setup_wizard import write_env

    if not provider:
        print("How should Mongoose be reachable when your home IP changes?\n")
        for i, p in enumerate(PROVIDERS, 1):
            tag = "  [recommended]" if p.slug == "tailscale" else ""
            print(f"  {i}. {p.name}{tag}")
            print(f"     {p.blurb}")
            if p.needs_portforward:
                print("     NOTE: needs a router port-forward, and exposes your home's public IP.")
            print()
        choice = _prompt("Choose 1-%d (or blank to cancel)" % len(PROVIDERS))
        if not choice.isdigit() or not (1 <= int(choice) <= len(PROVIDERS)):
            print("Cancelled.")
            return 1
        prov = PROVIDERS[int(choice) - 1]
    else:
        prov = provider_by_slug(provider)
        if not prov:
            print(f"Unknown provider '{provider}'. Options: " +
                  ", ".join(p.slug for p in PROVIDERS))
            return 1

    # Tailscale has its own installer/daemon path.
    if prov.kind == "vpn":
        return setup_tailscale(yes=yes)

    # DDNS providers: collect their keys.
    print(f"\nSetting up {prov.name}.")
    print(prov.setup_hint)
    if prov.needs_portforward:
        print("\nReminder: DDNS only keeps the NAME pointing at your IP. To actually reach Mongoose")
        print("you must (a) run `mongoose enable-phone` so it listens on the LAN, and (b) forward the")
        print("chosen port on your router to this Mac. DDNS also publishes your home's public IP.\n")

    updates = {"DDNS_PROVIDER": prov.slug, "DDNS_ENABLED": "true"}
    for key in prov.config_keys:
        label = key.replace("DDNS_", "").replace("_", " ").title()
        secret = any(s in key for s in ("TOKEN", "PASSWORD"))
        val = _prompt(label, secret=secret)
        if not val:
            print(f"  {label} is required; cancelling.")
            return 1
        updates[key] = val
    write_env(updates)
    print(f"\nSaved {prov.name} settings.")

    # do an immediate update so the user gets instant feedback
    print("Doing a first IP update...")
    res = update_now(force=True)
    if res.get("ok"):
        print(f"  OK, {res.get('detail','updated')} (current IP {res.get('ip','?')}).")
        print("Mongoose will keep this updated automatically (scheduler job 'ddns_update').")
        return 0
    print(f"  Update did not succeed: {res.get('detail')}")
    print("  Check the details and re-run: mongoose ddns setup")
    return 1
