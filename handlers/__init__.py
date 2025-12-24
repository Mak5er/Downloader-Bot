from aiogram import Router

from . import user, tiktok, youtube, admin, twitter

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router,
    youtube.router,
    admin.router,
    twitter.router,
)

__all__ = [
    router
]
