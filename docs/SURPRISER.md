# The Surpriser: Mongoose as a resident companion

The Surpriser lets Mongoose occasionally offer a genuinely useful, unprompted suggestion,
and stay quiet the rest of the time. It is the proactive layer on top of the Daily
Operating Picture: it watches for FRESH findings and cross-domain CORRELATIONS and, when
one is worth your attention, surfaces a single suggestion.

## The whole design is "earn the interruption"

The failure mode this avoids is the notification spammer. Every rule serves restraint:

- **Grounded**: suggestions come from real state changes and cross-domain correlations
  (an email from someone you're about to meet; weather that just turned against an outdoor
  plan; an unanswered message to someone in today's calendar). Never model riffing.
- **Novel**: each suggestion has a stable signature; the same one is never surfaced twice
  within a cooldown (~20h). It cannot nag.
- **Well-timed**: quiet hours (22:00–08:00 local) are respected; a minimum gap (~90 min)
  between surprises is enforced.
- **Selective**: even with several candidates, only the single highest-value novel one is
  offered, and only above a value threshold. Silence is the default outcome.

## Two modes

**On demand** (always available): `mongoose suggest`, or the cockpit Overview's "Mongoose
noticed → Anything for me?" button, or GET /cockpit/suggest. This asks right now (it
bypasses quiet-hours and the gap, but still never repeats a suggestion).

**Resident habit** (opt-in): set `MONGOOSE_SURPRISER_ENABLED=true` and run the scheduler.
Mongoose then checks periodically (default every 45 min) and pushes a suggestion to your
phone only when something fresh, correlated, novel, and well-timed turns up. Most cycles
are silent by design.

## Feedback

Each suggestion can be marked useful / not useful (cockpit buttons, or `mongoose suggest
--feedback not_useful --signature <sig>`). It's a light loop: feedback is recorded against
the suggestion; it does not pretend to be machine learning.

## Privacy and honesty

The surpriser inherits the DOP's privacy handling (high-privacy email subjects are never
surfaced). It is off by default: a resident assistant that interrupts you is opt-in. Its
value scales with how much real data is flowing; with an empty corpus it simply stays
quiet.
