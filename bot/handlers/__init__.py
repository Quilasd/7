"""Агрегация роутеров. Порядок важен: admin/testgame раньше прочих callback-роутеров."""

from aiogram import Router

from bot.handlers import (
    admin,
    game,
    game_chats,
    owner,
    groups_admin,
    history,
    profile,
    ratings,
    rewards,
    rooms,
    social,
    start,
    testgame,
    voting,
)


def get_root_router() -> Router:
    root = Router(name="root")
    root.include_router(admin.router)
    root.include_router(owner.router)
    root.include_router(testgame.router)
    root.include_router(rewards.router)
    root.include_router(groups_admin.router)
    root.include_router(ratings.router)
    root.include_router(start.router)
    root.include_router(profile.router)
    root.include_router(social.router)
    root.include_router(history.router)
    root.include_router(rooms.router)
    root.include_router(game.router)
    root.include_router(voting.router)
    root.include_router(game_chats.router)
    return root


__all__ = ["get_root_router"]
