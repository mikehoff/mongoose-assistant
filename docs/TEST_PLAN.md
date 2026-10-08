# Test Plan

## Test levels

### Unit tests

Test pure logic:

- weather summary selection;
- route preference update;
- notification classification;
- privacy surface selection;
- FTS insertion and search;
- calendar availability overlap.

### Integration tests

Test local service boundaries:

- FastAPI health endpoint;
- SQLite persistence;
- mocked Home Assistant call;
- mocked Alexa bridge call;
- mocked ntfy push;
- mocked weather provider.

### Manual acceptance tests

Test with real devices only after local logic passes:

- call a known Home Assistant entity;
- route a low-sensitivity notification to phone/watch;
- confirm private content does not go to Alexa;
- sync one IMAP mailbox with a small limit;
- index one test folder;
- test Siri Shortcut or Android voice webhook.

## Acceptance criteria

Mongoose is acceptable when:

- `/health` works;
- weather summary works from cache and refresh;
- home command logs HA attempt and fallback outcome;
- notifications are deduped and searchable;
- private notifications are not marked speakable;
- calendar availability returns expected busy blocks;
- IMAP/manual email search works;
- file search works;
- tests pass through `./scripts/qa_repo.sh`.

## Regression checklist

Before adding a new connector:

- Does it expose private content?
- Does it modify external systems?
- Can it be disabled through `.env`?
- Does it log errors without leaking tokens?
- Does it respect privacy class routing?
- Is it covered by a unit or integration test?
