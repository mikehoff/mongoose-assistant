# Onboarding: the single guided first-run journey

The hardest gap in Mongoose was never a missing capability, it was that the capabilities
weren't connected into a path a person could walk. Install was terminal-and-Mac-first;
pairing a phone was a tab you had to discover; there was no "you're set up, now put it on
your phone" moment. This is the spine that fixes that.

## What it is

A single guided flow, served at `/welcome`, that takes a person from "the app just
opened" to "it's running, my data is connecting, and my phone is paired". Three steps:

1. **Start the local brain** , Mongoose's model runs on the machine; this turns green on
   its own once Ollama is up.
2. **Connect your information** , calendar, email, messages, so Mongoose has something
   real to work with (optional; you can do one now and more later).
3. **Add your phone** , the handoff. The pairing QR is right here in the flow, not in a
   separate tab. Scan it and your phone becomes a Mongoose surface (approvals, voice, what
   changed). The page moves on by itself once the phone connects.

## It's a state machine, not a script

`/cockpit/onboarding` computes where the person is from REAL signals (is the brain up? is
anything connected? is a phone paired?). So it's resumable: quit halfway and reopen, and
you land on the step you were on, no lost place, no repeated steps. A phone-first person
isn't trapped on "connect".

## The double-click front door (macOS)

`scripts/make_macos_app.sh` builds `Mongoose.app`. Double-click it and it: runs the
one-time bootstrap (Homebrew/Python 3.12/Ollama/venv) in a visible Terminal on first run;
starts the local brain; starts the Mongoose server; and opens `/welcome`. No terminal
needed after that.

Honest limit: the `.app` launcher is straightforward shell, but it can only be exercised
on a real Mac, a Linux sandbox cannot run or verify a macOS bundle. Treat it as
Mac-validated. The onboarding FLOW itself (the web journey, the state machine) is fully
tested and verified.

## Still to come (named honestly)

This delivers the guided install, the signposting-to-phone, and the in-flow handoff. The
fourth piece of a fully transparent ecosystem , a single "system map" that shows a normal
person what's connected, what each device can see, and where their data lives , is not yet
built. The receipts ledger (Activity tab) shows what Mongoose has DONE; the system-map
view (what Mongoose IS) is the next step.
