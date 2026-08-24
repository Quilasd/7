"""Smoke-тест: приложение собирается целиком (DI, middleware, роутеры, recover)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_app(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("ADMIN_IDS", "111,222")

    from bot.config import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()
    assert settings.admin_id_list() == [111, 222]
    assert settings.is_admin(111)

    from bot.main import create_app

    bot, dp, services, timers = await create_app()
    try:
        assert dp is not None
        assert services.games is not None
        assert services.phases is not None
        # Активных игр нет — recover ничего не восстанавливает
        assert await services.phases.recover() == 0
    finally:
        timers.cancel_all()
        await bot.session.close()
        from bot.database.database import dispose_engine

        await dispose_engine(services.engine)
        reset_settings_cache()
