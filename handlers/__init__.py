from aiogram import Router

from . import (
    user,
    tiktok,
    youtube,
    spotify,
    admin,
    twitter,
    instagram,
    soundcloud,
    pinterest,
    threads,
    guest,
)
from .admin_history_dialog import admin_history_dialog

router = Router(name=__name__)

router.include_routers(
    user.router,
    guest.router,
    tiktok.router,
    youtube.router,
    spotify.router,
    admin.router,
    twitter.router,
    instagram.router,
    threads.router,
    soundcloud.router,
    pinterest.router,
    admin_history_dialog,
)

__all__ = [router]
