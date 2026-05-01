# Probe Tracing Design

**Goal:** Add code-level probe and recovery tracing so the sanitizer can be debugged easily in Docker/Synology while processing `original_playlist.m3u8`, without leaking raw provider URLs into logs.

## Scope

This change covers:

- structured runtime logging for full checks, per-channel probe attempts, retries, recovery runs, and publish decisions
- safe channel identifiers derived from playlist metadata plus a short URL fingerprint
- deployment validation with Docker against the existing `original_playlist.m3u8` input

This change does not add a separate log file sink or external logging service.

## Design

### Logging strategy

The runtime will keep using Python's standard `logging` module and write logs to container stdout/stderr. The emitted messages will be structured single-line events so they remain readable in `docker logs` and filterable in Synology Container Manager.

Event classes:

- cycle events: scheduler config, full check start/end, recovery run start/end
- channel events: probe start, attempt number, retry scheduled, probe success, probe failure, timeout
- publish events: candidate counts, guard decision, recovery publish trigger, Emby refresh outcome

Log levels:

- `INFO` for cycle summaries and publish decisions
- `DEBUG` for per-channel and per-attempt tracing
- `WARNING` for guard failures, Emby refresh failures, invalid config tokens, and final channel probe failures
- `ERROR` and exception logs for unexpected runtime faults

### Safe channel identifiers

Raw IPTV URLs must not be written to logs. Each channel event will carry:

- channel display name parsed from `#EXTINF` when available, otherwise a fallback like `unnamed-channel`
- short stable fingerprint derived from the URL, for example the first 10 hex chars of a SHA-1 digest

This gives enough identity for debugging repeated failures and recoveries without exposing provider hosts or tokens.

### Probe tracing

`app.probe` will log:

- probe batch start with channel count and probe settings
- per-channel attempt start at `DEBUG`
- per-channel retry scheduling with attempt counters and delay
- per-channel terminal outcome with success/failure reason and timeout marker
- batch summary with totals and retry successes

The probe API may need to carry richer per-channel context so runtime logs can include names and fingerprints without reparsing in multiple places.

### Recovery tracing

`app.main` will log:

- how many channels enter the offline set after a full check
- which channels are being retried in each recovery run
- which channels recovered and which remain offline
- when a recovery-triggered republish occurs

This makes the existing recovery behavior observable rather than implicit.

### Deployment and verification

For the first live run:

- build and start `playlist-sanitizer` with `LOG_LEVEL=DEBUG`
- confirm the container reads `original_playlist.m3u8`
- inspect logs to verify probe attempts, failures, retries, recovery scheduling, and publish output
- confirm the served clean playlist is updated only through the guard path

## Files likely to change

- `app/main.py`
- `app/probe.py`
- `README.md`
- `tests/test_main_smoke.py`
- new or expanded probe/runtime logging tests

## Risks and mitigations

- Too much debug output for 1000+ channels:
  mitigate by keeping detailed channel logs at `DEBUG` and summaries at `INFO`.
- Sensitive data leakage:
  mitigate by never logging raw URLs and using only names plus short fingerprints.
- Logging changes obscuring current behavior:
  mitigate with targeted unit tests around retries, failures, and recovery logging.
