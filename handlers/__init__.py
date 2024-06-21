from aiogram import Router
from . import user, tiktok

router = Router(name=__name__)

router.include_routers(
    user.router,
    tiktok.router
)

__all__ = [
    router
]