# Rajdhani display font mini spec

## Purpose

Rajdhani is the Sillygoose invitation/display voice: squared, quick, slightly futuristic, and warm enough for the playful guest journey. It should make the first guest screen feel like a real web app rather than a generic admin panel.

Mongoose must treat Rajdhani as a **display accent**, not a body font.

## Provisioning rule

Guest browsers must not request Google Fonts directly. Mongoose provisions Rajdhani once from the owner side, rewrites the CSS to local `/static/fonts/provisioned/rajdhani/...` URLs, caches the files under the persistent Mongoose data directory, and serves them locally afterwards.

The guest page loads:

```html
<link rel="stylesheet" href="/static/fonts/rajdhani.css">
```

If provisioning has not completed, the CSS falls back to `local('Rajdhani')` and then the bundled Inter face. The invitation must still load quickly and remain usable.

`MONGOOSE_FONT_PROVISIONING=off` disables remote fetching. `force=True` in the provisioning helper is reserved for explicit repair/dev actions.

## Where Rajdhani should be used

Use Rajdhani for short, high-energy display moments:

| Surface | Use | Notes |
|---|---|---|
| Guest invite hero | Main welcome line and primary buttons | Canonical use case. Uppercase, 600/700, slight letter spacing. |
| Guest confirmation screen | “Thank you. Enjoy Mongoose!” headline | Reinforces that the invite completed. |
| Device launchpad | Tiny mode labels such as `Talk`, `Status`, `Voice`, `Guest` | Use sparingly; keep chat text in Inter. |
| Cockpit guest cards | Guest invite code names, “Sillygoose”, role pills | Good for playful status, not long descriptions. |
| First-run celebration | One-line success headlines after HTTPS/DDNS/pairing works | Makes setup wins feel rewarding. |
| Notification titles | Very short local/web notification titles if rendered inside Mongoose UI | Do not assume OS notifications will use it. |
| Easter-egg/delight cards | Small headline only | Never use for multi-sentence facts. |

## Where Rajdhani should not be used

Do not use Rajdhani for:

- chat message bodies;
- email bodies or voicemail transcripts;
- logs, receipts, JSON, model output, or diagnostics;
- legal/RTM text, service-charge material, or anything requiring sober readability;
- paragraph copy longer than one sentence;
- small low-contrast helper text.

Use Inter for normal UI and Fraunces for owner-facing “instrument panel” headings. Use IBM Plex Mono for logs, IDs, diagnostics, and receipts.

## Style tokens

Recommended CSS:

```css
font-family: "Rajdhani", "Inter", system-ui, sans-serif;
font-weight: 600; /* 700 only for hero headlines */
letter-spacing: .03em;
text-transform: uppercase; /* only when the string is short */
```

Keep contrast AA-compliant. Rajdhani is condensed, so avoid using it below `14px` unless it is bold and high contrast.

## Product rule

Rajdhani should signal: “you are entering a playful Sillygoose surface.”

It should not take over Mongoose’s main voice. The main assistant still feels calm, useful, local, and trustworthy; Rajdhani is the wink at the door.
