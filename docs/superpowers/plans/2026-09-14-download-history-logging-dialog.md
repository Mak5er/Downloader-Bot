# Download History, Docker Log Noise Reduction, and Admin aiogram-dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean up noisy Docker/console logging into clear 1-2 line summaries per download, create a persistent `download_history` database table tracking who downloads what (with full URL, user details, status, duration, file size), and build an interactive, compact Telegram admin viewer using `aiogram-dialog` with pagination (15-20 links/page in expandable blockquotes), direct "Message by Chat ID" integration, and CSV export. All UI text and buttons must be strictly in English.

**Architecture:** 
1. Reconfigure logging so fine-grained internal telemetry (probe, sync, metrics, batch flush) is routed to `DEBUG` / structured JSONL files, while console/Docker stdout receives clean, formatted high-level `[REQUEST]`, `[SUCCESS]`, and `[FAILED]` summary events without duplicate token spam.
2. Introduce a `DownloadHistory` SQLAlchemy model and Alembic migration (`20260914_000013_add_download_history.py`) alongside an asynchronous `DownloadHistoryRepositoryMixin` to record download attempts across all media flows without latency penalties.
3. Integrate `aiogram-dialog==2.6.0` into the bot lifecycle, create an admin history browsing dialog with paginated windows, compact `<blockquote expandable>` formatting for each link, quick filters, direct `✉️ Message by Chat ID` integration, and CSV export. All user-facing text is English.

**Tech Stack:** Python 3.14, aiogram 3.31.0, aiogram-dialog 2.6.0, SQLAlchemy 2.0 (async), Alembic, PostgreSQL / SQLite, colorlog.

---

### Task 1: Docker & Console Log Noise Reduction

**Files:**
- Modify: `services/logger.py`
- Modify: `services/download/queue.py`
- Modify: `utils/download_manager.py`
- Modify: `handlers/media_download.py`
- Test: `tests/test_logger.py`

- [ ] **Step 1: Write test verifying console handler log levels and summary formatting**

In `tests/test_logger.py`, add tests verifying that internal queue/download telemetry (`queue_job_queued`, `download_probe`, `download_sync`, `analytics_batch_flush`) does not spam the console handler at `INFO` level, and that `log_media_request`, `log_media_success`, and `log_media_failure` emit clean, structured summaries.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_logger.py -k "console" -v`
Expected: FAIL (no filtering or demoting implemented yet).

- [ ] **Step 3: Implement clean console filtering and high-level download logging helpers**

In `services/logger.py`:
1. Demote micro-events in `download_manager.py`, `download_queue.py`, and `main.py` (like `queue_job_queued`, `download_probe`, `download_sync`, `analytics_batch_flush`, `flow_started`, `flow_completed`) from `INFO` to `DEBUG` for console output, while preserving them for `events_log.jsonl` / `perf_log.jsonl`.
2. Add a `ConsoleCleanFilter` to `_build_console_handler` that suppresses noisy internal telemetry from stdout.
3. Clean `CONSOLE_FORMAT` to display human-readable concise lines instead of repeating the same key-values twice in `text_context_block` and `text_message_block`.
4. Add helper methods to `ContextLoggerAdapter`:
   - `logger.download_request(user_id, username, service, url)`
   - `logger.download_success(user_id, service, file_type, size_bytes, duration_s, url)`
   - `logger.download_error(user_id, service, error, url)`

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_logger.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/logger.py services/download/queue.py utils/download_manager.py handlers/media_download.py tests/test_logger.py
git commit -m "refactor(logger): reduce Docker log noise and add concise download summary logging"
```

---

### Task 2: Database Schema & Migration for `download_history`

**Files:**
- Modify: `services/storage/models.py`
- Modify: `services/storage/schema.py`
- Create: `services/storage/download_history_repository.py`
- Modify: `services/storage/db.py`
- Create: `services/alembic/versions/20260914_000013_add_download_history.py`
- Create: `tests/test_download_history.py`
- Modify: `tests/test_migrations.py`

- [ ] **Step 1: Write tests for `download_history` model and repository methods**

In `tests/test_download_history.py`:
- Test creating a download record (`record_download`) with success, cached, and error statuses.
- Test querying paginated history (`get_download_history(page=1, per_page=15)`).
- Test filtering history by `user_id`, `service`, and `status`.
- Test `cleanup_expired_history(max_age_days=90)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_download_history.py -v`
Expected: FAIL (module/methods not found).

- [ ] **Step 3: Define `DownloadHistory` model and Alembic migration**

In `services/storage/models.py`:
```python
class DownloadHistory(Base):
    __tablename__ = "download_history"
    __table_args__ = (
        Index("ix_download_history_user_id", "user_id"),
        Index("ix_download_history_created_at", "created_at"),
        Index("ix_download_history_service", "service"),
        Index("ix_download_history_status", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    chat_id = Column(BigInteger, nullable=True)
    chat_type = Column(Text, nullable=True)
    service = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    title = Column(Text, nullable=True)
    file_type = Column(Text, nullable=True)
    file_id = Column(Text, nullable=True)
    file_size_bytes = Column(BigInteger, nullable=True)
    duration_seconds = Column(sa.Float, nullable=True)
    status = Column(Text, nullable=False, default="success")  # "success", "cached", "error"
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    user = relationship("User", primaryjoin="DownloadHistory.user_id == foreign(User.user_id)")
```
Add `"download_history"` to `APP_SCHEMA_TABLES`.
Create `services/storage/download_history_repository.py` with `DownloadHistoryRepositoryMixin` containing async methods.
Incorporate mixin into `DataBase` in `services/storage/db.py`.
Create migration `services/alembic/versions/20260914_000013_add_download_history.py` using batch operations for SQLite/PostgreSQL compatibility.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_download_history.py tests/test_migrations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/storage/ models.py schema.py db.py download_history_repository.py services/alembic/versions/ tests/test_download_history.py tests/test_migrations.py
git commit -m "feat(storage): add download_history table, repository mixin, and migration"
```

---

### Task 3: Pipeline Integration (Recording Downloads)

**Files:**
- Modify: `services/media/orchestration.py`
- Modify: `services/media/audio_flow.py`
- Modify: `services/media/delivery.py`
- Modify: `services/inline/send_flow.py`
- Modify: `handlers/media_download.py`
- Test: `tests/test_media_orchestration.py`
- Test: `tests/test_audio_flow.py`

- [ ] **Step 1: Write tests for recording download history in orchestration and delivery**

In `tests/test_media_orchestration.py`:
- Test that successful single-media download records a `success` history entry with size, duration, user_id, service, and url.
- Test that cache hits record a `cached` history entry.
- Test that failures record an `error` history entry.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_media_orchestration.py -k "history" -v`
Expected: FAIL.

- [ ] **Step 3: Implement `record_download` calls in orchestration, delivery, and inline send flow**

1. In `services/media/orchestration.py`:
   - Pass user context (`user_id`, `chat_id`, `chat_type`, `service`, `url`, `title`).
   - Call `db.record_download(...)` upon cached hit, fresh download success, or caught exception.
2. In `services/media/delivery.py`:
   - Record album/media items.
3. In `services/media/audio_flow.py`:
   - Record audio downloads and conversions.
4. In `services/inline/send_flow.py`:
   - Record inline and guest mode download deliveries.
5. In all records, log the clean summary message to stdout using `logger.download_success` / `logger.download_error`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_media_orchestration.py tests/test_audio_flow.py tests/test_media_delivery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/media/ services/inline/ handlers/media_download.py tests/
git commit -m "feat(download): record download history and summary logs across all media handlers"
```

---

### Task 4: Add `aiogram-dialog` Dependency & Application Setup

**Files:**
- Modify: `requirements.txt`
- Modify: `main.py`
- Test: `tests/test_main_startup.py`

- [ ] **Step 1: Write test verifying `setup_dialogs` is configured on Dispatcher**

In `tests/test_main_startup.py`, verify that dialog routers and middleware are registered when building the dispatcher.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main_startup.py -k "dialog" -v`
Expected: FAIL.

- [ ] **Step 3: Add `aiogram-dialog==2.6.0` to requirements and configure in `main.py`**

1. Add `aiogram-dialog==2.6.0` to `requirements.txt`.
2. Install into venv: `pip install aiogram-dialog==2.6.0`.
3. In `main.py`:
   - Import `setup_dialogs` from `aiogram_dialog`.
   - Include dialog routers into `dp`.
   - Call `setup_dialogs(dp)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main_startup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt main.py tests/test_main_startup.py
git commit -m "build(deps): add aiogram-dialog and wire into bot dispatcher"
```

---

### Task 5: Admin Download History Dialog (`aiogram-dialog`) with "Message by Chat ID" Integration

**Files:**
- Create: `handlers/admin_history_dialog.py`
- Modify: `handlers/admin.py`
- Modify: `keyboards/inline_keyboards.py`
- Create: `tests/test_admin_history_dialog.py`

- [ ] **Step 1: Write tests for `admin_history_dialog`**

In `tests/test_admin_history_dialog.py`:
- Test launching the dialog from admin menu (`bg.start(AdminHistorySG.history, mode=StartMode.RESET_STACK)`).
- Test getter data loading (pagination: page 1 of N, 15 items per page).
- Test next page / previous page transitions.
- Test service filter switching (e.g. All -> TikTok -> YouTube).
- Test English formatting: each link output in `<blockquote expandable>...</blockquote>` with user link, timestamp, service badge, status.
- Test "✉️ Message by Chat ID" integration: selecting a user or clicking the button transitions cleanly into the direct message flow.
- Test CSV export callback.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_admin_history_dialog.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `AdminHistorySG` and dialog window (English UI)**

In `handlers/admin_history_dialog.py`:
1. Define states:
   ```python
   class AdminHistorySG(StatesGroup):
       history = State()
   ```
2. Data getter:
   Fetch `page`, `per_page=15`, filter from dialog data.
   Query `db.get_download_history(...)` and `db.get_download_history_count(...)`.
   Format each entry into compact HTML with `<blockquote expandable>` (English):
   ```html
   <blockquote expandable>
   🎬 <b>{service_icon} {service_name}</b> | <a href="{url}">{title_or_link}</a>
   👤 {user_link} (<code>{user_id}</code>) | {status_badge}
   ⏱ <i>{created_at_fmt}</i> {details_str}
   </blockquote>
   ```
3. Window buttons (English):
   - Row 1: `[ ◀️ Prev ]` `[ {current_page} / {total_pages} ]` `[ Next ▶️ ]`
   - Row 2: Filter buttons `[ All ]` `[ Errors ❌ ]` `[ Platform 🔽 ]`
   - Row 3: `[ ✉️ Message by Chat ID ]` -> opens direct message input for a user or prompts for user ID.
   - Row 4: `[ 📊 Export CSV ]` -> sends CSV report to admin.
   - Row 5: `[ ⬅️ Back to Admin ]`
4. Wire button in `keyboards/inline_keyboards.py` and `handlers/admin.py`:
   Add `[ 📥 Download History ]` to `admin_keyboard()`.
5. Connect `✉️ Message by Chat ID`:
   When clicked in the dialog, allow quick direct messaging (e.g. prompting for message or pre-filling from selected user in history, using existing `Admin.write_chat_id` / `Admin.write_chat_text` flow).
6. CSV export handler:
   Generates a clean CSV file (`user_id,username,service,url,title,status,date`) for the selected filter and sends it via `answer_document`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_admin_history_dialog.py tests/test_admin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add handlers/admin_history_dialog.py handlers/admin.py keyboards/inline_keyboards.py tests/test_admin_history_dialog.py
git commit -m "feat(admin): implement download history viewer with aiogram-dialog, blockquotes, Message by Chat ID, and CSV export"
```

---

### Task 6: Full Integration Verification & Regression Testing

**Files:**
- All touched files
- Test: Full pytest suite

- [ ] **Step 1: Run full test suite**

Run: `pytest`
Expected: All tests pass (600+ tests).

- [ ] **Step 2: Run linter**

Run: `ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Verify migration with SQLite & PostgreSQL schemas**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS.

- [ ] **Step 4: Final commit and verification report**
