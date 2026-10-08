# Running Mongoose unattended (launchd)

The scheduler (daily brief, periodic email sync, file reindex) is a process that has to
keep running. To run it unattended on the Mac, install it as a launchd agent. It then
starts on login and restarts if it dies.

## Install

```
mongoose service generate   # writes the plist with your real paths (nothing to edit)
mongoose service install    # writes + loads it, after asking you to confirm
mongoose service status     # shows installed / loaded, and the log paths
```

`install` prints exactly what it will do and waits for confirmation before loading
(use `--yes` to skip the prompt in scripts).

What it sets up:
- Runs `mongoose scheduler` continuously, with KeepAlive so launchd restarts it if it
  exits.
- RunAtLoad, so it also starts at every login.
- Absolute paths filled in at generation time (the current interpreter / the installed
  `mongoose` script, and this repo as the working directory). There is nothing to
  hand-edit, which is the point: the old per-install plist with placeholder paths was
  the thing to avoid.

## Logs

Durable logs (they survive reboot, unlike /tmp):

```
data/logs/scheduler.out.log
data/logs/scheduler.err.log
```

View them with `tail -f`:

```
tail -f "$(mongoose service status | awk '/out log/{print $3}')"
```

or open them in Console.app.

## Uninstall

```
mongoose service uninstall   # unloads the agent and removes the plist
```

## Notes

- macOS only (launchd). On Linux you'd use a systemd user unit; not generated here.
- `launchctl load` needs a logged-in GUI user session (your user domain). If install
  reports a problem, confirm you're running it as your normal user while logged in.
- `mongoose release-check` reports whether the scheduler is running unattended, so you can
  confirm at a glance that daily operation is actually wired, not just available.
