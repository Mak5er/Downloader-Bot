from aiogram import Router

from . import user, tiktok, youtube, admin, twitter, instagram

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router,
    youtube.router,
    admin.router,
    twitter.router,
    instagram.router,
)

__all__ = [
    router
]
