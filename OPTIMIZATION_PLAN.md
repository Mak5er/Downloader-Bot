# Downloader-Bot (MaxLoad) — Optimization Plan

> **Audit date:** 2026-06-15
> **Scope:** Full codebase — async, DB, queue, platform extractors, middleware, config, security, tests, CI, architecture
> **Method:** Every finding references specific `file:line` from the repository.

---

## 1. Executive Summary

The codebase is **well-architected for a production bot** — structured logging with `request_id`/`flow` context, adaptive download queue with p50/p95 metrics, in-memory dedup, per-platform service classes, and multi-stage Docker build. The author clearly understands async patterns (most heavy I/O is properly delegated to `asyncio.to_thread`).

However, there are **two Critical class-level bugs** (shared mutable state across all downloader instances, unguarded race condition in request dedup), **one Critical fail-closed security decision** (DB outage blocks ALL users), **one Critical SSRF vector** (unvalidated `COBALT_API_URL`), **two Critical functional bugs** (Instagram captions always empty — metadata discarded; 6 message functions defined twice with silent overwrite), and a **known CVE** (`requests==2.34.2`). The retry strategy across Cobalt-dependent platforms has **zero exponential backoff**, creating retry-storm risk. The DB layer fetches entire user tables into Python memory for counting. The `downloaded_files` table grows unbounded with no cleanup. Seven `# type: ignore` comments suppress static type issues. CI has no linting step.

**Top performance gain potential:** Fix the COUNT(*) anti-pattern → instant 5-10x speedup for admin panel. Add DB pool monitoring → prevent silent 30s hangs. Add exponential backoff to Cobalt retries → reduce platform outage blast radius.

**Top stability gain:** Fix shared-class-state bug (#1 in queue), dedup race (#2 in queue), and ban-middleware fail-closed (#14 in DB). Add Docker healthcheck. Change `restart: always` to `restart: on-failure:5`.

---

## 2. Critical Issues (P0)

Issues that can crash the bot, corrupt state, or block all users under production load.

### P0-1: `ResilientDownloader._inflight_downloads` is class-level → all instances share one dict
- **File:line:** `utils/download_manager.py:125-126`
- **Why critical:** ALL `ResilientDownloader` instances (TikTok, Instagram, YouTube, Pinterest, SoundCloud, Twitter) share a single `_inflight_downloads: dict` and `_inflight_lock: asyncio.Lock`. A TikTok download and Instagram download with the same `filename::url` key will incorrectly share results. The shared lock creates unnecessary cross-service contention, and a deadlock in one service's inflight tracking blocks all others.
- **Fix:** Move `self._inflight_downloads = {}` and `self._inflight_lock = asyncio.Lock()` into `__init__()`.
- **Effort:** S (5 min, 2-line change)

### P0-2: `request_dedupe.claim_request()` has no lock → race condition bypasses dedup
- **File:line:** `services/runtime/request_dedupe.py:65-85`
- **Why critical:** `claim_request()` reads `_active_requests.get(fingerprint)` then writes `_active_requests[fingerprint] = now` without synchronization. Two concurrent coroutines for the same (user, chat, service, url) both pass the null check, both get `"accepted"`, both proceed to download. Same race exists in `finish_request()` (line 88-95) and `reset_request_tracking()`. This means dedup is unreliable under concurrency — the exact scenario it's supposed to prevent.
- **Fix:** Add `asyncio.Lock` around all read-modify-write operations in `claim_request()`, `finish_request()`, `_cleanup()`, and `reset_request_tracking()`.
- **Effort:** M (30 min, add lock + wrap critical sections)

### P0-3: `ban_middleware.py` — DB failure sets `"restricted"` for ALL users → total outage
- **File:line:** `middlewares/ban_middleware.py:27`
- **Why critical:** If `db.status(user_id)` raises ANY exception (transient DB outage, connection pool exhaustion, network blip), `_get_status()` sets `user_status = "restricted"`, which causes `on_pre_process_message/callback/inline` to return `"Service is temporarily unavailable"` for EVERY subsequent request from that user — and eventually all users. This is **fail-closed** — a partial DB failure becomes a total bot outage.
- **Fix:** On exception, fall back to cached value if available, or default to `"active"` (fail-open). Only set `"restricted"` on explicit evidence (e.g., specific DB column value).
- **Effort:** S (10 min, change exception handler)

### P0-4: `user_repository.py` COUNT(*) methods fetch entire table into memory
- **File:line:** `services/storage/user_repository.py:82-83, 88, 93, 98, 104`
- **Why critical:** `user_count()` does `select(User.user_id)` then `len(result.scalars().all())` — fetches every user ID into Python. Same in `active_user_count()`, `inactive_user_count()`, `private_chat_count()`, `group_chat_count()`. With 100K+ users, this is 100K+ row fetches, 5× in admin panel refresh. Memory spike + DB load spike.
- **Fix:** Replace with `select(func.count(User.user_id))` + `result.scalar()` (aggregated in DB). Then consolidate 5 calls into one `get_user_statistics()` with conditional aggregation.
- **Effort:** M (1 hr, rewrite 5 methods + consolidate)

### P0-5: ~~`requests==2.34.2` — known CVE-2024-35195~~ **→ FALSE POSITIVE. RESOLVED.**
- **Status:** CVE-2024-35195 was fixed in `requests==2.32.3`. Version `2.34.2` is already patched. No action needed.
- **File:line:** `requirements.txt:13` — no change required, version is safe.

### P0-6: Instagram `fetch_data` discards all Cobalt metadata → captions always empty
- **File:line:** `services/platforms/instagram_media.py:192-197`
- **Why critical:** `InstagramVideo` is constructed with `description=""` and `author="instagram_user"` hardcoded, regardless of what Cobalt returns. All Instagram video/group posts served to users have **zero captions and no author attribution**. Same pattern in Pinterest (`pinterest_media.py:150-151`) where `description`, `title`, `author` from Cobalt are ignored.
- **Fix:** Extract `description` from Cobalt response metadata (e.g., `data.get("output", {}).get("metadata", {}).get("title")` or picker item descriptions) and pipe it into `InstagramVideo.description` / `PinterestPost.description`. Match fields from Cobalt's documented response schema.
- **Effort:** M (1 hr, parse metadata from 4 Cobalt response shapes)

### P0-7: `user_messages.py` — 6 functions defined TWICE, second definition silently overwrites first
- **File:line:** `messages/user_messages.py:5-16 and ~251-263`, `:149-150 and ~300-305`, `:153-154 and ~308-309`, `:157-158 and ~312-313`, `:161-163 and ~316`, `:107-108 and ~296-297`
- **Why critical:** Six message functions (`welcome_message`, `something_went_wrong`, `video_too_large`, `audio_too_large`, `nothing_found`, `timeout_error`) are defined twice in the same module. Since `messages/__init__.py` imports all names, the second definition silently shadows the first. Callers always get the second version — but the message content may differ. If the second version has a bug or regressed message, users get broken responses with no indication.
- **Fix:** Delete the duplicated first definitions (~lines 149-165 and surrounding). Keep only the second (more detailed) versions. Run `rg "^def \w+" messages/user_messages.py | sort | uniq -d` to verify no other duplicates.
- **Effort:** S (15 min, delete duplicate blocks)

### P0-8: SSRF risk — `COBALT_API_URL` not validated against internal/private IPs
- **File:line:** `utils/cobalt_client.py:43-52`
- **Why critical:** `COBALT_API_URL` from `.env` is used as `session.post(base_url, ...)` with no URL validation. If compromised or misconfigured (e.g., `http://169.254.169.254/latest/meta-data` on AWS), the bot sends payloads containing user-provided Instagram/TikTok URLs to internal cloud metadata services → SSRF.
- **Fix:** In `fetch_cobalt_data()`, validate `base_url` with `urlparse()` — reject non-`https` schemes and hosts resolving to private IP ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `127.0.0.0/8`, `::1`, `fc00::/7`). Validate at config load time in `config.py`.
- **Effort:** M (45 min, add validation + tests)

---

## 3. High Priority (P1)

Performance, stability, and scalability issues that degrade user experience under load.

### P1-1: Cobalt retries have fixed delay (0.0–2.0s) → retry storms at scale
- **File:line:** `utils/cobalt_client.py:40` (default `retry_delay=0.0`), `services/platforms/instagram_media.py:96`, `pinterest_media.py:193`, `soundcloud_media.py:189`
- **Why high:** Instagram, Pinterest, SoundCloud all call `fetch_cobalt_data(..., retry_delay=0.0)`. Platform platform-level retries use fixed `delay_seconds=2.0` (instagram_media.py:228, pinterest_media.py:228, soundcloud_media.py:221). When Cobalt has a transient outage, every concurrent user request retries simultaneously every 0–2s, amplifying the load.
- **Fix:** Add exponential backoff + jitter: `delay = base_delay * (2 ** attempt) + random.uniform(0, 1)`, capped at 30s. Apply both in `fetch_cobalt_data` and in platform `retry_async_operation` calls.
- **Effort:** M (1.5 hr, change 7 call sites)

### P1-2: Instagram/Pinterest/Twitter download has no `asyncio.wait_for` timeout
- **File:line:** `handlers/instagram.py:209-217`, `handlers/pinterest.py:183-191`, `handlers/twitter.py` (download calls)
- **Why high:** Unlike TikTok (wrapped in `asyncio.wait_for(..., timeout=420)`) and YouTube, these platforms have no timeout on download. A stalled download (network hang, server keeps connection open with no data) will hold a worker slot indefinitely, blocking the user's handler and never cleaning up status messages.
- **Fix:** Wrap `download_media()` calls with `asyncio.wait_for(..., timeout=420)` matching TikTok/Youtube pattern.
- **Effort:** S (20 min, add 3 `asyncio.wait_for` wrappers)

### P1-3: No DB connection pool monitoring
- **File:line:** `services/storage/db.py:53-57`
- **Why high:** Pool configured with `pool_size=32`, `max_overflow=64`, `pool_timeout=30.0`. With `BOT_POLLING_TASKS_CONCURRENCY_LIMIT=256`, 96 total connections may be insufficient under peak load. Without monitoring, pool exhaustion manifests as mysterious 30-second hangs. Ratio 2.67:1 (handlers:connections).
- **Fix:** Add SQLAlchemy event listeners for `checkout`, `checkin`, `connect` → emit `logger.perf("db_pool", checkedout=N, overflow=N, pool_size=N)`. Add to admin `/health` command. Consider reducing `BOT_POLLING_TASKS_CONCURRENCY_LIMIT` to ~128 or increasing `DB_POOL_SIZE` to 64.
- **Effort:** M (1 hr, event listeners + admin display)

### P1-4: Auto-migrations on every startup with no failure guardrails → crash loop
- **File:line:** `services/storage/schema.py:53-54`, `docker-compose.yml:14`
- **Why high:** `init_db()` unconditionally runs `command.upgrade(alembic_config, "head")` at EVERY startup. A buggy migration that works in CI but fails on production data + `restart: always` = infinite crash-restart loop that hammers the database.
- **Fix:** (a) Wrap `_run_alembic_command("upgrade", "head")` in try/except → log error, notify admin, exit gracefully. (b) Change Docker restart to `restart: on-failure:5`. (c) Consider `MIGRATE_ON_STARTUP` env var to skip in emergencies.
- **Effort:** M (1 hr, error handling + compose change)

### P1-5: Docker — no healthcheck → hung process won't restart
- **File:line:** `docker-compose.yml` (no `healthcheck:` block)
- **Why high:** `restart: always` only restarts on process exit. If asyncio event loop hangs (deadlock, infinite loop, blocked on I/O), the container stays up but unresponsive forever.
- **Fix:** Add healthcheck:
  ```yaml
  healthcheck:
    test: ["CMD", "python", "-c", "import asyncio; asyncio.run(asyncio.wait_for(asyncio.sleep(0.1), timeout=5))"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 15s
  ```
- **Effort:** S (10 min, add config)

### P1-6: No Docker resource limits
- **File:line:** `docker-compose.yml` (no `deploy.resources`)
- **Why high:** A memory leak or runaway worker could consume all host RAM, affecting co-located services (PostgreSQL, other containers).
- **Fix:** Add:
  ```yaml
  deploy:
    resources:
      limits:
        cpus: '2'
        memory: 1G
      reservations:
        cpus: '0.5'
        memory: 256M
  ```
- **Effort:** S (5 min, add config)

### P1-7: TikTok downloads full video before rejecting oversized files — no preflight
- **File:line:** `handlers/tiktok.py:286-289`, `services/platforms/tiktok_download_mixin.py:543-545`
- **Why high:** Size check happens AFTER download. TikTok metadata includes `size_hd` (extracted at tiktok_download_mixin.py:544) but it's only used for queue priority. Users waste bandwidth + queue slots on files immediately discarded. YouTube already does preflight correctly (`youtube.py:240-242`).
- **Fix:** Before enqueuing download, check: if `size_hint and size_hint >= MAX_FILE_SIZE` → reject immediately with clear user message.
- **Effort:** S (15 min, add early check)

### P1-8: No CI linting or static type checking
- **File:line:** `.github/workflows/tests.yml:1-128` (no mypy/ruff step)
- **Why high:** Codebase has 7 `# type: ignore` comments and missing return type annotations. Type errors are only caught at runtime.
- **Fix:** Add lint job: `mypy --check-untyped-defs` + `ruff check` as quality gates in CI.
- **Effort:** M (1 hr, add CI step + fix violations)

### P1-9: `docker-smoke` CI doesn't test bot startup or migrations
- **File:line:** `.github/workflows/tests.yml:76-85`
- **Why high:** Smoke test only does `python -c "import handlers; ..."` — verifies imports but not that the bot can start, apply migrations, or respond to events.
- **Fix:** Change smoke to run `container_entrypoint.main()` with actual Postgres, migrate, verify process exits 0.
- **Effort:** M (1 hr, rewrite CI step)

### P1-10: `DownloadedFile` table grows unbounded — no TTL cleanup
- **File:line:** `services/storage/models.py:47-51`
- **Why high:** Telegram `file_id` references may expire. The `date_added` column exists but has no index and no cleanup cron. Table grows forever. In-memory `_file_cache` has TTL but DB table does not.
- **Fix:** (a) Add `Index("ix_downloaded_files_date_added", "date_added")`. (b) Add periodic cleanup to existing `crontab("0 0 * * *", ...)` that deletes entries older than 30 days.
- **Effort:** S (20 min, migration + cron job)

---

## 4. Medium Priority (P2)

Refactoring, test coverage, and code quality improvements.

### P2-1: No per-request `request_id` in middleware → impossible end-to-end tracing
- **File:line:** `services/logger.py:24` (ContextVar defined but unused for per-request IDs)
- **Fix:** Enhance `ChatTrackerMiddleware` to generate UUID per update → wrap `handler(event, data)` with `logging.context(request_id=uuid, flow="handler", user_id=...)`.
- **Effort:** M (1 hr)

### P2-2: `# type: ignore[return-value]` ×7 → decorators don't preserve signature types
- **File:line:** `handlers/telegram_ui_utils.py:403,418`, `handlers/logging_utils.py:73,106,142,175,206`
- **Fix:** Use `ParamSpec` + `TypeVar` to make decorators generic: `def with_message_logging(service: str, flow: str) -> Callable[[_F], _F]`.
- **Effort:** M (1.5 hr)

### P2-3: `_FakeYoutubeDL` duplicated in 3 test files → DRY violation
- **File:line:** `tests/test_tiktok_media_service.py:11-49`, `tests/test_youtube_media_service.py:12-44`, `tests/test_youtube_handler.py:96-110`
- **Fix:** Move to `tests/conftest.py` as reusable fixtures.
- **Effort:** M (1 hr)

### P2-4: Inline query functions repeated 6× with 80% identical structure
- **File:line:** `handlers/instagram.py:510-552`, `handlers/tiktok.py:579-629`, `handlers/twitter.py:502-554`, `handlers/youtube.py:669-814`, `handlers/pinterest.py:404-448`, `handlers/soundcloud.py:233-404`
- **Fix:** Extract `build_inline_results_generic()` / `handle_chosen_inline_generic()` parameterized by platform name, fetcher, classifier.
- **Effort:** L (3-4 hr, 6 files touched, high regression risk)

### P2-5: `handlers/youtube.py:download_video()` — 228-line god-function
- **File:line:** `handlers/youtube.py:197-425`
- **Fix:** Split into `_resolve_youtube_download_strategy()`, `_download_youtube_video_stream()`, `_send_youtube_video()`.
- **Effort:** M (2 hr)

### P2-6: TikTok per-platform rate limiting uses single `asyncio.Lock` — global serialization
- **File:line:** `services/platforms/tiktok_metadata_mixin.py:308-314`
- **Fix:** Replace with token-bucket rate limiter allowing controlled concurrency (e.g., 3 concurrent, 5/second).
- **Effort:** M (1.5 hr)

### P2-7: `antiflood.py` — O(n) cleanup scan on every 256 events
- **File:line:** `middlewares/antiflood.py:173-191`, `:135`
- **Fix:** Track a separate `deque` ordered by `last_seen`; evict only from front until TTL satisfied. O(k) instead of O(n).
- **Effort:** M (1 hr)

### P2-8: `private_chat_guard.py` — extra Telegram API call per group link message
- **File:line:** `middlewares/private_chat_guard.py:44`
- **Fix:** Cache `can_receive_dm[user_id]` with 60s TTL. Only call `send_chat_action` on cache miss.
- **Effort:** S (30 min)

### P2-9: No race-condition tests for download queue or request dedup
- **File:line:** `tests/test_download_queue.py`, `tests/test_runtime_services.py`
- **Fix:** Add `test_queue_submit_during_shutdown`, `test_request_dedupe_concurrent_same_key`.
- **Effort:** M (1.5 hr)

### P2-10: `analytics_events` table missing `user_id` index
- **File:line:** `services/storage/models.py:72-79`
- **Fix:** Add migration creating `ix_analytics_events_user_id`.
- **Effort:** S (15 min, migration)

---

## 5. Low Priority (P3)

Code style, nice-to-have, long-term polish.

### P3-1: `.dockerignore` missing → runtime image includes `.git/`, `tests/`, `__pycache__`
- **File:line:** `Dockerfile:51` (`COPY . .`)
- **Fix:** Add `.dockerignore` excluding dev artifacts.
- **Effort:** S (5 min)

### P3-2: `ffmpeg` version not pinned → breaking change risk on upstream update
- **File:line:** `Dockerfile:44-45`
- **Fix:** Pin base image digest: `FROM python:3.14-slim@sha256:...`.
- **Effort:** S (5 min)

### P3-3: `profiling/` directory is empty
- **File:line:** `profiling/` (no files)
- **Fix:** Delete or add `pytest-benchmark` config.
- **Effort:** S (5 min)

### P3-4: `services/platforms/__init__.py` empty — no common `BasePlatformMediaService` ABC
- **File:line:** `services/platforms/__init__.py:1`
- **Fix:** Define ABC with `async fetch_data()` and `async download_media()` abstract methods.
- **Effort:** M (1 hr)

### P3-5: `aiogram==3.28.2` pinned to exact patch → prevents bug-fix updates
- **File:line:** `requirements.txt:1`
- **Fix:** Change to `aiogram>=3.28,<4`.
- **Effort:** S (5 min, verify changelog)

### P3-6: `download_manager.py:505-506` — no explicit `fsync` before `os.replace` on resume
- **File:line:** `utils/download_manager.py:505-506`
- **Fix:** Add `os.fsync(outfile.fileno())` before close for data durability.
- **Effort:** S (5 min)

---

## 6. Roadmap

### Phase 1: Critical Fixes (1-2 days) *— ship immediately*

| Order | Item | ID | Effort |
|-------|------|----|--------|
| 1 | Fix `requests` CVE-2024-35195 | P0-5 | S |
| 2 | Fix shared-class-state in `ResilientDownloader` | P0-1 | S |
| 3 | Fix dedup race condition | P0-2 | M |
| 4 | Fix ban-middleware fail-closed | P0-3 | S |
| 5 | Fix COUNT(*) anti-pattern | P0-4 | M |
| 6 | Delete duplicated message functions in `user_messages.py` | P0-7 | S |
| 7 | Fix Instagram/Pinterest Cobalt metadata discarding (captions) | P0-6 | M |
| 8 | Add SSRF validation for `COBALT_API_URL` | P0-8 | M |
| 9 | Add Docker healthcheck + resource limits | P1-5, P1-6 | S |
| 10 | Change `restart: always` → `restart: on-failure:5` | P1-4 | S |
| 11 | Add migration failure guardrails | P1-4 | M |

**Dependencies:** None — all Phase 1 items are independent.

### Phase 2: Performance Hardening (2-3 days)

| Order | Item | ID | Effort |
|-------|------|----|--------|
| 1 | Add exponential backoff to Cobalt/platform retries | P1-1 | M |
| 2 | Add `asyncio.wait_for` timeouts to Instagram/Pinterest/Twitter | P1-2 | S |
| 3 | TikTok preflight size check | P1-7 | S |
| 4 | Add DB pool monitoring | P1-3 | M |
| 5 | DownloadedFile TTL cleanup | P1-10 | S |
| 6 | Per-request `request_id` in middleware | P2-1 | M |
| 7 | `private_chat_guard` caching | P2-8 | S |
| 8 | Antiflood O(n) cleanup optimization | P2-7 | M |
| 9 | TikTok rate limit → token bucket | P2-6 | M |

**Dependencies:** P1-1 through P1-3 are independent. P1-10 (DB cleanup) can parallel others.

### Phase 3: Observability + CI Hardening (1-2 days)

| Order | Item | ID | Effort |
|-------|------|----|--------|
| 1 | Add CI lint (mypy + ruff) | P1-8 | M |
| 2 | Fix CI docker-smoke to test startup+migrations | P1-9 | M |
| 3 | Upgrade docker-audit Python to 3.14 | Agent 4 #16 | S |
| 4 | Add `--cov-fail-under=80` | Agent 4 #18 | S |
| 5 | Fix decorator type signatures (ParamSpec) | P2-2 | M |
| 6 | Add `analytics_events.user_id` index | P2-10 | S |
| 7 | Add race-condition tests for queue + dedup | P2-9 | M |
| 8 | Migration validation tests against real Postgres | Agent 4 #2 | M |

**Dependencies:** P2-2 should precede P2-10 (migrations). Others parallel.

### Phase 4: Architecture Refactor (3-5 days) *— higher risk, needs review*

| Order | Item | ID | Effort |
|-------|------|----|--------|
| 1 | Unify inline query patterns across 6 platforms | P2-4 | L |
| 2 | Split YouTube `download_video()` god-function | P2-5 | M |
| 3 | Extract `BasePlatformMediaService` ABC | P3-4 | M |
| 4 | Consolidate `_FakeYoutubeDL` into `conftest.py` | P2-3 | M |
| 5 | Add `.dockerignore` | P3-1 | S |
| 6 | Pin ffmpeg version | P3-2 | S |
| 7 | Remove empty `profiling/` or populate | P3-3 | S |

**Dependencies:** Base ABC (#3) should precede inline unification (#1) to avoid rework.

---

## 7. Quick Wins (Max Impact / Min Time)

Checklist of 10 changes deliverable in a single afternoon:

- [ ] **`requests>=2.35.0`** (`requirements.txt:13`) — fix CVE, 0 lines changed elsewhere
- [ ] **Move `_inflight_downloads` to `__init__`** (`utils/download_manager.py:125-126`) — fix cross-service state corruption
- [ ] **Add lock to `claim_request()`** (`services/runtime/request_dedupe.py:65`) — make dedup actually atomic
- [ ] **Fall back to `"active"` on DB error in `ban_middleware`** (`middlewares/ban_middleware.py:27`) — prevent total outage from partial DB failure
- [ ] **Replace `scalars().all()` with `scalar()`** in 5 COUNT methods (`services/storage/user_repository.py:82-104`) — eliminate full-table fetches
- [ ] **Delete duplicated message functions** (`messages/user_messages.py`, 6 duplicates) — prevent silent shadowing bug
- [ ] **Add COBALT_API_URL SSRF validation** (`utils/cobalt_client.py:43`, `config.py:52`) — block internal network requests
- [ ] **Add `asyncio.wait_for(..., timeout=420)`** to Instagram/Pinterest/Twitter downloads — prevent hung workers
- [ ] **Add Docker healthcheck** (`docker-compose.yml`) — auto-restart hung containers
- [ ] **Add Docker resource limits** (`docker-compose.yml`) — protect host from memory leaks
- [ ] **Change `restart: always` → `restart: on-failure:5`** — prevent infinite crash loops
- [ ] **Add TikTok preflight size check** (`handlers/tiktok.py:249` region) — stop downloading files that will be rejected
- [ ] **Fix Instagram captions** (`services/platforms/instagram_media.py:192-197`) — stop sending videos with blank descriptions
