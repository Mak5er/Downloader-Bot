from aiogram import Router

from . import user, tiktok, instagram, youtube, admin

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router,
    instagram.router,
    youtube.router,
    admin.router,
)

__all__ = [
    router
]
