from aiogram import Router

from . import user, tiktok, instagram, youtube, admin, twitter, full_quality

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router,
    instagram.router,
    youtube.router,
    admin.router,
    twitter.router,
    full_quality.router,
)

__all__ = [
    router
]
