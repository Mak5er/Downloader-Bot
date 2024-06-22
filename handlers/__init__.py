from aiogram import Router

from . import user, tiktok, instagram

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router,
    instagram.router,
)

__all__ = [
    router
]
