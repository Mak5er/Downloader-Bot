# Design Specification: User & Group Separation, Reach Tracking, and Admin Improvements

**Date:** 2026-09-08  
**Status:** Approved by User  
**Scope:** Downloader-Bot (`services/storage`, `middlewares`, `handlers/admin`, `handlers/commands`, `messages`)

---

## 1. Overview & Problem Statement

### 1.1 Current Architecture Flaws
Currently, all Telegram entities (individual users with positive IDs and group chats with negative IDs) are stored inside a single `users` table with a column `chat_type` set to `"private"` or `"public"`.

When any member writes a message in a group where the bot is present:
1. `middlewares/chat_tracker.py` executes `_ensure_group(chat)` and immediately calls `_ensure_user(user, "private")` with `status="active"`.
2. As a result, random group chatters who never initiated a private conversation (DM) with the bot are falsely inserted into `users` with `chat_type="private"` and `status="active"`.
3. When an administrator clicks **Check active users** in the admin panel, the bot queries `SELECT * FROM users` and attempts `bot.send_chat_action(chat_id=user_id, action="typing")` for every entry.
4. Telegram Bot API rejects these calls with `403 Forbidden: bot can't initiate conversation with a user`, causing the bot to mark every such user as `status="inactive"`.
5. In production, 79% (484 out of 615) of "inactive" users never performed a single interaction with the bot.
6. The broadcast feature (`send_to_all`) indiscriminately sends messages to all rows in `users`, blasting both groups and unreachable group members, wasting rate limits.
7. The bot lacks group audience tracking (`member_count`), so there is no visibility into how many people potentially see media sent in groups.

### 1.2 Target Goals
1. **Clean Entity Separation:**
   - Real users who have direct private chat with the bot (`has_dm = True`).
   - Group participants seen in chats (`has_dm = False`) tracked without polluting private DM stats.
   - Distinct `groups` table with official member counts (`bot.get_chat_member_count`).
2. **Many-to-Many Group Membership (`group_members`):**
   - Track which groups a user participates in.
   - If a user is active in multiple groups, they are deduplicated in global community reach (`COUNT(DISTINCT user_id)`), while remaining properly linked to each group.
3. **Accurate DM Reachability Checks:**
   - `check_active_users` evaluates **only** users with `has_dm = True`.
   - Groups and group-only members are never marked as `inactive` private users.
4. **Group Reach & Health Checks:**
   - Track `member_count` for each group and compute total potential audience reach across active groups ($\sum \text{member\_count}$).
   - Detect when the bot has been kicked from a group (`kicked` status).
5. **Segmented Admin Broadcast (`send_to_all`):**
   - Admin can target: Private chats only, Groups only, or Both.
6. **All Texts in English:**
   - Admin panel messages, menus, and notifications remain strictly in English.

---

## 2. Database Schema & Models

### 2.1 Table: `users`
Represents unique Telegram users (`user_id > 0`).

```python
class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_user_username", "user_username"),
        Index("ix_users_status", "status"),
        Index("ix_users_has_dm", "has_dm"),
    )

    user_id = Column(BigInteger, primary_key=True, autoincrement=False)
    user_name = Column(Text, nullable=True)
    user_username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True, default="private")
    language = Column(Text, nullable=True)
    has_dm = Column(Boolean, nullable=False, default=False, server_default=sa.text("false"))
    status = Column(Text, nullable=True, default="active")  # "active", "inactive", "ban"
    referred_by = Column(BigInteger, nullable=True)
    source = Column(Text, nullable=True)

    settings = relationship("Settings", back_populates="user", uselist=False)
```

- For existing users with `user_id > 0` and `chat_type == "private"`, `has_dm` will default to `True` during migration to preserve historical data.
- When new group messages arrive from non-DM users, `has_dm` remains `False`.

### 2.2 Table: `groups`
Dedicated table for group chats (`id < 0`).

```python
class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        Index("ix_groups_status", "status"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    title = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    chat_type = Column(Text, nullable=True)  # "group", "supergroup"
    status = Column(Text, nullable=False, default="active")  # "active", "kicked", "left"
    member_count = Column(BigInteger, nullable=False, default=0, server_default=sa.text("0"))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
```

### 2.3 Table: `group_members`
Many-to-Many junction table linking users to the groups where they were observed.

```python
class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        Index("ix_group_members_user_id", "user_id"),
        Index("ix_group_members_group_id", "group_id"),
    )

    group_id = Column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    last_seen_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
```

### 2.4 Table: `settings` Constraint Adjustment
`Settings.user_id` currently stores either a user ID or a group chat ID (when group admins customize download settings).
- To allow group settings without requiring groups to exist in `users`, the foreign key constraint `fk_settings_user_id_users` is dropped in Alembic.
- The `UniqueConstraint("user_id", name="uq_settings_user_id")` is preserved.

---

## 3. Migration Plan (Alembic)

A new revision `20260908_000011_user_group_separation.py` will:
1. Add column `has_dm` (boolean, default false) to `users`.
2. Populate `has_dm = true` for existing rows where `user_id > 0` and `chat_type = 'private'`.
3. Create table `groups` (`id`, `title`, `username`, `chat_type`, `status`, `member_count`, `created_at`, `updated_at`).
4. Migrate existing group records (rows in `users` where `user_id < 0`) into `groups`:
   - `id = user_id`, `title = user_name`, `username = user_username`, `chat_type = 'group'`, `status = COALESCE(status, 'active')`.
5. Remove migrated negative ID rows from `users` (or retain safely if referenced, after dropping FK).
6. Create table `group_members` (`group_id`, `user_id`, `last_seen_at`) with primary key `(group_id, user_id)` and foreign keys to `groups(id)` and `users(user_id)`.
7. Drop foreign key constraint on `settings(user_id)` referencing `users(user_id)` so both user and group settings can persist cleanly.

---

## 4. Middlewares & Handlers Workflow

### 4.1 `middlewares/chat_tracker.py`
```python
if chat_type_value == "private":
    if user and not user.is_bot:
        await self._ensure_user(user, has_dm=True)
else:
    await self._ensure_group(chat)
    if user and not user.is_bot:
        # Save user identity without granting DM active status
        await self._ensure_user(user, has_dm=False)
        # Record membership in the group
        await self._ensure_group_member(chat_id=chat.id, user_id=user.id)
```

- `_ensure_group`:
  - Upserts into `groups`.
  - Caches `member_count` with a 1-hour TTL to prevent Telegram API flood limits.
  - Queries `bot.get_chat_member_count(chat.id)` when cache expires.
- `_ensure_user`:
  - If `has_dm=True`, sets `has_dm=True` and `status="active"`.
  - If `has_dm=False`, only updates user name/username if the user exists or creates the profile with `has_dm=False`. If the user already had `has_dm=True`, it is preserved!

### 4.2 `handlers/commands.py:update_info`
- Guard check:
  ```python
  if message.chat.type == ChatType.PRIVATE:
      await db.upsert_user(..., has_dm=True, status="active")
  ```

### 4.3 `handlers/commands.py:handle_bot_membership`
- When bot joins a group or gains permissions (`MEMBER` / `ADMINISTRATOR`):
  - Queries `member_count = await bot.get_chat_member_count(chat.id)`.
  - Upserts `groups` with `status="active"` and `member_count`.
- When bot is removed (`KICKED` / `LEFT` / `RESTRICTED`):
  - Updates `groups` with `status="kicked"`.

---

## 5. Admin Panel & Statistics

### 5.1 Admin Dashboard Template (`messages/admin_messages.py`)
```python
def admin_panel(
    dm_total, dm_active, dm_inactive, dm_banned,
    groups_total, groups_active, groups_inactive,
    total_reach, tracked_group_members
):
    return (
        "<b>Hello, this is the admin panel.</b>\n\n"
        "👤 <b>Private Chats (DM):</b>\n"
        "  ├ Total with DM: <b>{dm_total}</b>\n"
        "  ├ ✅ Active (reachable): <b>{dm_active}</b>\n"
        "  ├ 🚫 Inactive (blocked bot): <b>{dm_inactive}</b>\n"
        "  └ ⛔ Banned: <b>{dm_banned}</b>\n\n"
        "🏘 <b>Groups:</b>\n"
        "  ├ Total groups: <b>{groups_total}</b>\n"
        "  ├ ✅ Active (bot member): <b>{groups_active}</b>\n"
        "  ├ 🚫 Inactive (kicked/left): <b>{groups_inactive}</b>\n"
        "  ├ 👥 <b>Estimated Reach:</b> <b>~{total_reach:,} members</b>\n"
        "  └ 💬 Tracked chatters: <b>{tracked_group_members}</b>"
    ).format(
        dm_total=dm_total,
        dm_active=dm_active,
        dm_inactive=dm_inactive,
        dm_banned=dm_banned,
        groups_total=groups_total,
        groups_active=groups_active,
        groups_inactive=groups_inactive,
        total_reach=total_reach,
        tracked_group_members=tracked_group_members,
    )
```

### 5.2 Active DM Verification (`check_active_users`)
- Query:
  ```python
  users_to_check = await db.get_users_for_reachability_check()
  # SELECT * FROM users WHERE has_dm = TRUE AND status != 'ban'
  ```
- Groups and `has_dm = False` users are **never** pinged.

### 5.3 Group Health & Member Count Check (`check_groups`)
- Query active groups from `groups`.
- For each group, call `await bot.get_chat_member_count(group.id)`:
  - Success: update `member_count` and keep `status="active"`.
  - `TelegramForbiddenError` or `TelegramBadRequest` ("chat not found"): update `status="kicked"`.

### 5.4 Segmented Mailing (`send_to_all`)
- Provide inline keyboard to select audience:
  1. `[👤 Private chats only]` (`has_dm = True AND status = 'active'`)
  2. `[🏘 Groups only]` (`groups.status = 'active'`)
  3. `[🌐 All (DM + Groups)]`
- Display audience preview with recipient counts before broadcasting.

---

## 6. Testing & Verification

1. **Alembic Migrations:**
   - Test upgrade on the local test PostgreSQL container with the restored real production database.
   - Verify `groups`, `group_members`, and `users` data integrity.
2. **Unit & Integration Tests:**
   - Update `tests/test_chat_tracker.py` to assert that group messages link user to group and do not set `has_dm = True`.
   - Update `tests/test_admin.py` to assert `check_active_users` only checks `has_dm = True` users.
   - Test group member count retrieval and audience calculation.
3. **Full Pytest Suite:**
   - Ensure all existing and new tests pass with 100% test integrity.
