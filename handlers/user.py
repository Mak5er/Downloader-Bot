
import asyncio  # noqa: F401
from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest  # noqa: F401
from aiogram.filters import Command

import keyboards as kb  # noqa: F401
import messages as bm  # noqa: F401
from app_context import bot, db, send_analytics  # noqa: F401
from services.logger import logger as logging  # noqa: F401
from services.download.queue import get_download_queue  # noqa: F401
from services.runtime.pending_requests import pop_pending  # noqa: F401
from services.stats.chart import _render_stats  # noqa: F401

from config import BATCH_LINKS_MAX_ITEMS

from handlers import commands as cmd_mod
from handlers import media_download as media_mod
from handlers import settings_menu as settings_mod

router = Router(name=__name__)

_UPDATE_INFO_TTL_SECONDS = cmd_mod._UPDATE_INFO_TTL_SECONDS
_update_info_cache = cmd_mod._update_info_cache
_MAX_BATCH_LINKS = max(1, int(BATCH_LINKS_MAX_ITEMS))
_MESSAGE_NOT_MODIFIED_MARKERS = (
    "message is not modified",
    "specified new message content and reply markup are exactly the same",
)

# Route definitions
router.message(Command("start"))(cmd_mod.send_welcome)
router.message(Command("help"))(cmd_mod.send_help)
router.my_chat_member()(cmd_mod.handle_bot_membership)
router.message(Command("remove_keyboard"))(cmd_mod.remove_reply_keyboard)
router.message(Command("stats"))(cmd_mod.stats_command)
router.callback_query(F.data.startswith("stats:"))(cmd_mod.switch_stats)
router.callback_query(F.data.startswith("date_"))(cmd_mod.switch_period)

router.message(Command("settings"))(settings_mod.settings_menu)
router.callback_query(F.data == "back_to_settings")(settings_mod.back_to_settings)
router.callback_query(F.data.startswith("settings_cat:"))(settings_mod.open_category)
router.callback_query(F.data.startswith("settings:"))(settings_mod.open_setting)
router.callback_query(F.data.startswith("setting:"))(settings_mod.change_setting)
router.callback_query(F.data == "noop")(settings_mod.noop_callback)

router.message(media_mod._has_multiple_supported_links)(media_mod.process_batch_links)
router.callback_query(F.data == "start_supported_sites")(media_mod.show_supported_sites)

# Export functions & attributes for backwards compatibility
send_welcome = cmd_mod.send_welcome
send_help = cmd_mod.send_help
handle_bot_membership = cmd_mod.handle_bot_membership
remove_reply_keyboard = cmd_mod.remove_reply_keyboard
stats_command = cmd_mod.stats_command
switch_stats = cmd_mod.switch_stats
switch_period = cmd_mod.switch_period
update_info = cmd_mod.update_info
_extract_start_payload = cmd_mod._extract_start_payload
_build_pending_private_message = cmd_mod._build_pending_private_message

settings_menu = settings_mod.settings_menu
back_to_settings = settings_mod.back_to_settings
open_setting = settings_mod.open_setting
change_setting = settings_mod.change_setting
noop_callback = settings_mod.noop_callback
_admin_statuses = settings_mod._admin_statuses
_is_group_admin = settings_mod._is_group_admin
_is_message_not_modified_error = settings_mod._is_message_not_modified_error
_settings_chat_name = settings_mod._settings_chat_name
_ensure_settings_entities = settings_mod._ensure_settings_entities

process_batch_links = media_mod.process_batch_links
show_supported_sites = media_mod.show_supported_sites
_process_inline_album_deeplink = media_mod._process_inline_album_deeplink
_process_pending_message = media_mod._process_pending_message
_process_supported_link = media_mod._process_supported_link
_has_multiple_supported_links = media_mod._has_multiple_supported_links
_resolve_batch_concurrency = media_mod._resolve_batch_concurrency
_process_batch_links_parallel = media_mod._process_batch_links_parallel
