# Probe Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe per-channel probe/retry/recovery tracing to the sanitizer runtime, then deploy the Docker service and verify it processes `original_playlist.m3u8`.

**Architecture:** Keep the existing scheduler and publish flow, but enrich the probe pipeline with safe channel metadata so logs can identify channels without exposing raw IPTV URLs. Emit structured runtime events to container stdout and drive verification through pytest plus a live Docker run at `LOG_LEVEL=DEBUG`.

**Tech Stack:** Python 3.12, pytest, Docker Compose, ffprobe/ffmpeg, standard library logging

---

## File Structure

- Modify: `app/main.py`
  - derive safe channel names and fingerprints from playlist entries
  - log cycle-level and recovery-level runtime events
- Modify: `app/probe.py`
  - add probe target normalization and structured per-channel logging
- Modify: `README.md`
  - document debug deployment flow
- Modify: `tests/test_main_smoke.py`
  - cover safe metadata extraction and runtime log behavior
- Modify: `tests/test_probe.py`
  - cover retry and timeout logging behavior

### Task 1: Add failing probe logging tests

**Files:**
- Modify: `tests/test_probe.py`
- Test: `tests/test_probe.py`

- [ ] **Step 1: Write the failing test**

```python
def test_probe_channel_logs_retry_and_success(monkeypatch, caplog):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_probe.py`
Expected: FAIL because probe logging metadata is not emitted yet

- [ ] **Step 3: Write minimal implementation**

```python
logger.debug(...)
logger.warning(...)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_probe.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_probe.py app/probe.py
git commit -m "feat: add probe tracing logs"
```

### Task 2: Add failing runtime metadata tests

**Files:**
- Modify: `tests/test_main_smoke.py`
- Test: `tests/test_main_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_probe_targets_creates_safe_metadata(tmp_path: Path):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/test_main_smoke.py`
Expected: FAIL because safe probe target metadata helpers do not exist yet

- [ ] **Step 3: Write minimal implementation**

```python
def extract_channel_name(...):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q tests/test_main_smoke.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_main_smoke.py app/main.py
git commit -m "feat: add safe runtime channel metadata"
```

### Task 3: Wire runtime logs and recovery traces

**Files:**
- Modify: `app/main.py`
- Modify: `app/probe.py`
- Test: `tests/test_main_smoke.py`
- Test: `tests/test_probe.py`

- [ ] **Step 1: Add full-check and recovery trace logs**

```python
LOG.info("full check starting entries=%d", ...)
LOG.info("recovery check complete ...", ...)
```

- [ ] **Step 2: Add per-channel safe labels to probe flow**

```python
ProbeTarget(...)
ProbeResult(...)
```

- [ ] **Step 3: Run focused tests**

Run: `python -m pytest -q tests/test_main_smoke.py tests/test_probe.py`
Expected: PASS

- [ ] **Step 4: Refactor only if needed**

```python
def format_channel_label(...):
    ...
```

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/probe.py tests/test_main_smoke.py tests/test_probe.py
git commit -m "feat: trace probe retries and recovery runs"
```

### Task 4: Document and live verify Docker behavior

**Files:**
- Modify: `README.md`
- Test: `docker-compose.yml`

- [ ] **Step 1: Document debug run instructions**

```markdown
LOG_LEVEL=DEBUG docker compose up -d --build playlist-sanitizer
docker logs ...
```

- [ ] **Step 2: Run full verification**

Run: `python -m compileall -q app tests`
Expected: PASS

Run: `python -m pytest -q tests`
Expected: PASS

- [ ] **Step 3: Deploy and inspect runtime**

Run: `LOG_LEVEL=DEBUG docker compose up -d --build playlist-sanitizer`
Expected: container starts successfully

Run: `docker compose ps playlist-sanitizer`
Expected: service is running

Run: `docker logs --tail 200 playlist-sanitizer`
Expected: visible probe/retry/recovery/publish events without raw URLs

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document probe debug workflow"
```
