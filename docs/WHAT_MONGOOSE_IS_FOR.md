# What Mongoose is for

Most of Mongoose's parts have a better-resourced competitor. HA Assist controls
your home. Your mail client searches mail. Any chatbot chats. If Mongoose were
just "a local assistant that also does those things," it would lose to each of
them on its own turf.

Mongoose's actual reason to exist is the one thing none of those can do: it is
the only thing that sits across **all** your streams at once, calendar, email,
tasks, weather, home state, your remembered preferences, and the agent bus,
locally and privately, and synthesises across them.

- Your calendar app knows your 2pm moved. It does not know it now clashes with
  the dentist, or that it will rain while you cycle there.
- Your mail client knows an email arrived. It does not know it concerns a task
  that is already overdue.
- HA knows a device's state. It knows nothing about your inbox or your week.

Mongoose knows all of it, so it can tell you the thing that matters.

## The brief

`mongoose brief` is the USP made concrete: a cross-domain synthesis you don't
have to ask for. It:

1. Gathers the current picture from every domain (best-effort; a dead source
   never sinks the brief).
2. Computes the cross-domain signals **deterministically**, calendar conflicts
   (timezone-aware), rain against today's events, overdue/due-today tasks,
   waiting agent messages, so the model is given ground truth and cannot invent
   a conflict that isn't there or miss one that is.
3. Asks the resident model to write 2-4 sentences in Mongoose's voice, leading
   with whatever is time-sensitive.

If the model is unavailable, a deterministic fallback brief is still produced
from the signals alone, so the feature degrades to "still useful," not "broken."

```
mongoose brief            # print it now
mongoose brief --post     # also drop it on the bus as a message to you
```

## Make it proactive

The brief is most valuable when it's waiting for you, not when you remember to
ask. Schedule it (macOS launchd):

1. Edit paths in `scripts/com.mongoose.brief.plist` to your install.
2. `cp scripts/com.mongoose.brief.plist ~/Library/LaunchAgents/`
3. `launchctl load ~/Library/LaunchAgents/com.mongoose.brief.plist`

It runs `scripts/brief_schedule.sh` at 07:00 daily (change Hour/Minute), which
posts the brief to the bus. Combined with the conversation agent, you can then
ask Assist "what's my brief?" and hear it.

## Why this is the right place to invest

Every future Mongoose feature should be judged against this: does it deepen the
cross-domain picture (more signals, better synthesis, more sources feeding the
brief), or is it a single-domain feature something else already does better? The
brief is the spine; new capabilities earn their place by feeding it.
