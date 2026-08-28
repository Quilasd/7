"""Форумные темы партии: Game Topic и Mafia Topic.

АРХИТЕКТУРА (4 уровня, не смешиваются):
- 🏠 основная группа — обычное сообщество, без игровых сообщений;
- 🎮 Game Forum (ПОСТОЯННЫЙ форумный чат ГРУППЫ, ТЗ-11) → тема
  «🎮 Игра #N — <room>» на каждую партию: общение живых игроков днём,
  анонсы фаз; ночью закрыта;
- 🌙 Mafia Forum (ПОСТОЯННЫЙ форумный чат ГРУППЫ) → тема «🌙 Игра #N — <room>»
  на каждую партию: общение ЖИВОЙ мафии ночью; днём закрыта;
- 🤖 ЛС с ботом — роли, ночные действия, дневное голосование (кнопки
  в темы НЕ переносятся).

ФОРУМЫ PER-GROUP (ТЗ-11): каждая группа настраивает свою пару форумов в
group_settings (game_forum_chat_id / mafia_forum_chat_id, миграция 0009).
Игра группы использует ТОЛЬКО форумы своей группы — глобальные
GAME_FORUM_CHAT_ID/MAFIA_FORUM_CHAT_ID остаются fallback-ом исключительно
для игр БЕЗ группы (комнаты из личного чата с ботом).

Контекст темы = (chat_id форума, message_thread_id) — уникален для игры,
параллельные партии изолированы даже внутри одного форума.

TELEGRAM BOT API (проверено, фиктивного кода нет):
- createForumTopic — бот-админ с правом can_manage_topics создаёт тему
  автоматически при старте партии (ручных команд больше не нужно);
- closeForumTopic / reopenForumTopic — закрытие/открытие темы:
  закрытая тема не принимает сообщений вовсе (ночь для Game Topic, день
  для Mafia Topic, финал игры);
- per-topic прав ОТДЕЛЬНОМУ пользователю в API НЕТ (restrictChatMember
  действует на весь чат сразу) — поэтому основной механизм изоляции
  СЕРВЕРНАЯ проверка + удаление сообщения (enforce_message);
- deleteForumTopic существует, но не используется: темы остаются историей.

phase_manager остаётся источником истины: сервис подписывается на его
переходы. Все Telegram-операции best-effort: сбой тем НЕ ломает игру.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Game, GameStatus, PlayerStatus
from bot.database.repositories.games import GamePlayerRepository
from bot.roles import Team, team_of
from bot.utils.helpers import esc

logger = logging.getLogger(__name__)

ACTIVE_PHASES = (
    GameStatus.STARTING.value,
    GameStatus.NIGHT.value,
    GameStatus.DAY.value,
    GameStatus.VOTING.value,
)

# фазы, когда в теме партии можно писать живым (день и голосование)
DAY_TALK_PHASES = (GameStatus.DAY.value, GameStatus.VOTING.value)


# ------------------------------------------------------------------ gateway


class GameChatGateway(Protocol):
    """Тонкая обёртка над Telegram Bot API (для тестов — фейк)."""

    async def create_topic(self, chat_id: int, name: str, icon_color: int | None = None) -> int | None:
        """Создать тему в форумном чате -> message_thread_id."""

    async def close_topic(self, chat_id: int, thread_id: int) -> bool: ...

    async def reopen_topic(self, chat_id: int, thread_id: int) -> bool: ...

    async def send(self, chat_id: int, text: str, thread_id: int | None = None) -> bool: ...

    async def delete_message(self, chat_id: int, message_id: int) -> bool: ...

    async def chat_info(self, chat_id: int) -> dict | None:
        """getChat: {title, type, is_forum, ...} для проверки форумов."""


class TelegramGameChatGateway:
    """Реальный Telegram Bot API. Каждая операция — best-effort.

    Требуемые права бота в форумах: can_manage_topics (создание/закрытие тем).
    """

    # разрешённые цвета тем: 0x6FB9F0, 0xFFD67E, 0xFF93B2, 0xFB6F5F,
    # 0x8EEE98, 0xFFD67E — используем фиксированные для игровых/мафии
    GAME_TOPIC_COLOR = 0x6FB9F0
    MAFIA_TOPIC_COLOR = 0xFB6F5F

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _safe(self, coro_factory, what: str):
        try:
            return await coro_factory()
        except Exception as exc:  # TelegramAPIError и любые другие
            # права отозвали после настройки — типичный случай; бот НЕ падает,
            # игра продолжается, администратору вернёт право через /setup
            text = str(exc).lower()
            if "not enough rights" in text or "chat_admin_required" in text:
                logger.warning(
                    "GameChat: не удалось %s — у бота нет прав "
                    "(администратор/управление темами). Попросите админа вернуть "
                    "право и повторить /setup. Детали: %s",
                    what, exc,
                )
            else:
                logger.warning("GameChat: не удалось %s: %s", what, exc)
            return None

    async def create_topic(
        self, chat_id: int, name: str, icon_color: int | None = None
    ) -> int | None:
        result = await self._safe(
            lambda: self.bot.create_forum_topic(
                chat_id=chat_id, name=name[:128], icon_color=icon_color
            ),
            f"create_topic({chat_id})",
        )
        return getattr(result, "message_thread_id", None) if result else None

    async def close_topic(self, chat_id: int, thread_id: int) -> bool:
        return await self._safe(
            lambda: self.bot.close_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id
            ),
            f"close_topic({chat_id}, {thread_id})",
        ) is not None

    async def reopen_topic(self, chat_id: int, thread_id: int) -> bool:
        return await self._safe(
            lambda: self.bot.reopen_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id
            ),
            f"reopen_topic({chat_id}, {thread_id})",
        ) is not None

    async def send(self, chat_id: int, text: str, thread_id: int | None = None) -> bool:
        kwargs = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return await self._safe(
            lambda: self.bot.send_message(**kwargs), f"send({chat_id}/{thread_id})"
        ) is not None

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return await self._safe(
            lambda: self.bot.delete_message(chat_id=chat_id, message_id=message_id),
            f"delete_message({chat_id}, {message_id})",
        ) is not None

    async def chat_info(self, chat_id: int) -> dict | None:
        chat = await self._safe(
            lambda: self.bot.get_chat(chat_id), f"get_chat({chat_id})"
        )
        if chat is None:
            return None
        return {
            "title": getattr(chat, "title", None) or "",
            "type": getattr(chat, "type", ""),
            "is_forum": bool(getattr(chat, "is_forum", False)),
        }


class NoopGameChatGateway:
    """Заглушка: Telegram недоступен (тесты/локально) — операции тихо пропускаются."""

    async def create_topic(self, chat_id, name, icon_color=None) -> int | None:
        return None

    async def close_topic(self, chat_id: int, thread_id: int) -> bool:
        return False

    async def reopen_topic(self, chat_id: int, thread_id: int) -> bool:
        return False

    async def send(self, chat_id: int, text: str, thread_id: int | None = None) -> bool:
        return False

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return False

    async def chat_info(self, chat_id: int) -> dict | None:
        return None


class ForumProvider(Protocol):
    """Резолвер форумов партии по группе игры (ТЗ-11).

    get_for(session, group_id) -> (game_forum, mafia_forum):
    - group_id не None — форумы ИЗ group_settings ЭТОЙ группы (никаких
      глобальных fallback: группа A никогда не использует форумы группы B
      и глобальные env-форумы);
    - group_id None (игра без группы, ЛС-комнаты) — глобальные форумы
      (owner-настройка поверх .env).
    """

    async def get_for(
        self, session, group_id: int | None
    ) -> tuple[int | None, int | None]: ...


class StaticForumProvider:
    """Глобальные форумы из конфигурации (для тестов — фиксированные ID).

    Игра БЕЗ группы получает эту пару; игры групп получают (None, None) —
    per-group форумы в тестах настраиваются в group_settings.
    """

    def __init__(self, game_forum: int | None, mafia_forum: int | None) -> None:
        self.game_forum = game_forum
        self.mafia_forum = mafia_forum

    async def get_for(
        self, session, group_id: int | None
    ) -> tuple[int | None, int | None]:
        if group_id is None:
            return self.game_forum, self.mafia_forum
        return None, None


class DbForumProvider:
    """Форумы: для игры группы — group_settings этой группы; для игры без
    группы — глобальные (owner-настройка из БД поверх .env).

    ТЗ-11: глобальные env-форумы НЕ источник для игр групп — только fallback
    игр без группы.
    """

    def __init__(self, app_config, env_settings) -> None:
        self.app_config = app_config
        self.env_settings = env_settings

    async def _global(self) -> tuple[int | None, int | None]:
        try:
            gs = await self.app_config.get()
            return (
                gs.game_forum_chat_id
                or getattr(self.env_settings, "game_forum_chat_id", None),
                gs.mafia_forum_chat_id
                or getattr(self.env_settings, "mafia_forum_chat_id", None),
            )
        except Exception:
            logger.warning("GameChat: не удалось прочитать форумы из конфига")
            return (
                getattr(self.env_settings, "game_forum_chat_id", None),
                getattr(self.env_settings, "mafia_forum_chat_id", None),
            )

    async def get_for(
        self, session, group_id: int | None
    ) -> tuple[int | None, int | None]:
        if group_id is None:
            return await self._global()
        # per-group: только group_settings этой группы, БЕЗ глобального fallback
        try:
            from bot.database.repositories.groups import GroupSettingsRepository

            settings = (
                await GroupSettingsRepository(session).get_for(group_id)
                if session is not None
                else None
            )
        except Exception:
            logger.warning("GameChat: не удалось прочитать форумы группы %s", group_id)
            return None, None
        if settings is None:
            return None, None
        return settings.game_forum_chat_id, settings.mafia_forum_chat_id


# ------------------------------------------------------------------ service


class GameChatService:
    """Управление темами партий в двух постоянных форумах.

    Истина фаз — PhaseManager; сервис только следует его событиям.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        gateway: GameChatGateway,
        notifier=None,
        forums=None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.notifier = notifier
        self.forums = forums or StaticForumProvider(None, None)

    # ------------------------------------------------------------ темы партии

    async def on_game_started(self, session, game: Game, players) -> None:
        """Старт партии: автоматически создать обе темы (без ручных команд).

        Форумы резолвятся ПО ГРУППЕ игры (ТЗ-11): group_settings группы,
        для игры без группы — глобальные. Игрокам НЕ нужен отдельный доступ —
        они уже участники форума; тема просто появляется. Если форумы не
        настроены — тихо пропускаем.
        """
        game_forum, mafia_forum = await self._forums_for(session, game.group_id)
        if not game_forum and not mafia_forum:
            logger.info(
                "Игра %s: форумы не настроены (группа %s) — темы не создаются",
                game.id, game.group_id,
            )
            return

        room_name = await self._room_name(session, game)
        label = (room_name or "Игра").strip()[:24]
        failed = False
        if game_forum:
            name = f"🎮 Игра #{game.id} — {label}"
            thread_id = await self.gateway.create_topic(
                game_forum, name, TelegramGameChatGateway.GAME_TOPIC_COLOR
                if isinstance(self.gateway, TelegramGameChatGateway) else None
            )
            if thread_id:
                game.game_chat_id = game_forum
                game.game_thread_id = thread_id
                await self.gateway.send(
                    game_forum,
                    "🎮 <b>Тема партии создана.</b>\n\n"
                    f"Игра #{game.id} начинается. Обсуждение днём — здесь, "
                    "голосование и действия — в личном чате с ботом.",
                    thread_id,
                )
                logger.info("Игра %s: Game Topic %s/%s", game.id, game_forum, thread_id)
            else:
                failed = True
                logger.warning("Игра %s: не удалось создать Game Topic", game.id)
        if mafia_forum:
            name = f"🌙 Игра #{game.id} — {label}"
            thread_id = await self.gateway.create_topic(mafia_forum, name)
            if thread_id:
                game.mafia_chat_id = mafia_forum
                game.mafia_thread_id = thread_id
                logger.info("Игра %s: Mafia Topic %s/%s", game.id, mafia_forum, thread_id)
            else:
                failed = True
                logger.warning("Игра %s: не удалось создать Mafia Topic", game.id)

        if failed:
            # ТЗ §6: права могли отозвать после настройки — бот не падает,
            # партия продолжается в ЛС; администраторам группы — понятная
            # ошибка с предложением вернуть право и повторить /setup.
            await self._notify_topics_failed(session, game)
        await session.flush()

    async def on_night_started(self, session, game: Game, players) -> None:
        """-> NIGHT: тема игры закрывается, тема мафии открывается живой мафии."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        if game.game_chat_id and game.game_thread_id:
            await self.gateway.send(
                game.game_chat_id,
                "🌙 <b>НАСТУПИЛА НОЧЬ</b>\n\n"
                f"Ночь {game.day_number}\n"
                "💤 Игровая тема временно закрыта.\n"
                "🔮 Ночные действия — в личном чате с ботом.",
                game.game_thread_id,
            )
            await self.gateway.close_topic(game.game_chat_id, game.game_thread_id)
        if game.mafia_chat_id and game.mafia_thread_id:
            await self.gateway.send(
                game.mafia_chat_id,
                "🌙 <b>НОЧЬ МАФИИ</b>\n\n"
                "Обсуждайте жертву здесь. Выбор цели — в личном чате с ботом.",
                game.mafia_thread_id,
            )
            await self.gateway.reopen_topic(game.mafia_chat_id, game.mafia_thread_id)
        logger.info("Игра %s: ночной режим тем применён", game.id)

    async def on_day_started(self, session, game: Game, players) -> None:
        """-> DAY: тема игры открывается живым, тема мафии закрывается."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        if game.game_chat_id and game.game_thread_id:
            await self.gateway.send(
                game.game_chat_id,
                "☀️ <b>НАСТУПИЛ ДЕНЬ</b>\n\n"
                f"День {game.day_number}\n"
                "💬 Живые игроки могут общаться.\n"
                "🗳 Голосование — в личном чате с ботом.",
                game.game_thread_id,
            )
            await self.gateway.reopen_topic(game.game_chat_id, game.game_thread_id)
        if game.mafia_chat_id and game.mafia_thread_id:
            await self.gateway.send(
                game.mafia_chat_id,
                "☀️ Наступил день — тема мафии закрыта до следующей ночи.",
                game.mafia_thread_id,
            )
            await self.gateway.close_topic(game.mafia_chat_id, game.mafia_thread_id)
        logger.info("Игра %s: дневной режим тем применён", game.id)

    async def on_death(self, session, game: Game, gp) -> None:
        """Смерть: серверная блокировка применяется мгновенно (enforce_message).

        Отдельного Telegram-механизма нет: per-topic прав у API не существует
        (restrictChatMember закрыл бы пользователю ВЕСЬ форум). Мёртвый не
        может писать в темы игры — его сообщения удаляет модерация.
        """
        logger.info(
            "Игра %s: игрок %s умер — серверная блокировка тем (права API не требуются)",
            game.id, gp.user_id,
        )

    async def on_game_ended(self, session, game: Game, players, title: str) -> None:
        """🏁 Финал: анонс в обе темы, обе закрываются навсегда (история)."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        if game.game_chat_id and game.game_thread_id:
            await self.gateway.send(
                game.game_chat_id,
                f"🏁 <b>ИГРА ЗАВЕРШЕНА!</b>\n\n🏆 Победила команда: {esc(title or game.winner or '—')}\n\n"
                "Тема закрыта и сохранена как история партии.\n"
                "Подробности — в личке у бота: /history",
                game.game_thread_id,
            )
            await self.gateway.close_topic(game.game_chat_id, game.game_thread_id)
        if game.mafia_chat_id and game.mafia_thread_id:
            await self.gateway.send(
                game.mafia_chat_id,
                "🏁 Игра завершена. Тема мафии закрыта, история сохранена.",
                game.mafia_thread_id,
            )
            await self.gateway.close_topic(game.mafia_chat_id, game.mafia_thread_id)
        logger.info("Игра %s: темы партии закрыты (конец игры)", game.id)

    # ------------------------------------------------------------ модерация

    async def context_for(
        self, session, chat_id: int, thread_id: int | None
    ) -> tuple[Game, str] | None:
        """(chat_id, message_thread_id) -> (игра, 'game'|'mafia').

        Контекст = форум + тема. Игра ищется ТОЧНО по своей теме — параллельные
        партии в одном форуме не смешиваются. Завершённые игры тоже
        резолвятся (kind='closed'): в их темах писать нельзя.

        thread_id is None — сообщение ВНЕ тем партий: общая (General) тема
        форума или обычное сообщение супергруппы. Telegram НЕ присылает
        message_thread_id для сообщений General-темы, поэтому None — это
        «не игровая тема», а не «любая тема»: раньше такой запрос матчил
        ЛЮБУЮ игру этого чата (game_thread_id IS NOT NULL) и завершённая
        партия «отравляла» весь основной чат — бот удалял там сообщения
        навсегда (ложный «вечный мут» после конца игры).
        """
        if thread_id is None:
            return None
        stmt = select(Game).where(
            Game.game_chat_id == chat_id,
            Game.game_thread_id == thread_id,
        )
        game = (await session.execute(stmt)).scalars().first()
        if game is not None:
            return game, "game"
        stmt = select(Game).where(
            Game.mafia_chat_id == chat_id,
            Game.mafia_thread_id == thread_id,
        )
        game = (await session.execute(stmt)).scalars().first()
        if game is not None:
            return game, "mafia"
        return None

    async def chat_kind(self, session, chat_id: int, thread_id: int | None = None) -> str | None:
        """Какой АКТИВНОЙ игре принадлежит контекст (для middleware)."""
        found = await self.context_for(session, chat_id, thread_id)
        if found is None:
            return None
        game, kind = found
        return kind if game.status in ACTIVE_PHASES else None

    async def enforce_message(
        self, session, chat_id: int, thread_id: int | None, user,
        message_id: int, *, is_command: bool = False,
    ) -> bool:
        """Серверная проверка сообщения в форумной теме (основная изоляция).

        Возвращает True, если сообщение удалено (к хендлерам не идёт).
        Правила:
        - тема не активной игры (в т.ч. завершённой) — только чтение;
        - Game Topic: писать могут ЖИВЫЕ участники этой игры и только днём;
        - Mafia Topic: только ЖИВЫЕ мафиози этой игры и только ночью;
        - ночь/мёртвый/неучастник/игрок другой игры — сообщение удаляется.
        """
        found = await self.context_for(session, chat_id, thread_id)
        if found is None:
            return False  # не тема партии — обычная обработка
        game, kind = found

        if game.status not in ACTIVE_PHASES:
            await self.gateway.delete_message(chat_id, message_id)
            return True  # тема завершённой игры — только история

        gp = await GamePlayerRepository(session).get_by_user(game.id, user.id)
        if gp is None:
            await self.gateway.delete_message(chat_id, message_id)
            return True  # неучастник (в т.ч. игрок другой партии)

        allowed = False
        if kind == "mafia":
            allowed = (
                game.status == GameStatus.NIGHT.value
                and gp.is_alive
                and team_of(gp.role) == Team.MAFIA
            )
        else:  # тема игры
            allowed = gp.is_alive and game.status in DAY_TALK_PHASES

        if allowed:
            return False
        await self.gateway.delete_message(chat_id, message_id)
        logger.info(
            "GameChat: удалено сообщение %s в %s-теме игры %s (user %s)",
            message_id, kind, game.id, user.id,
        )
        return True

    # ------------------------------------------------------------ recover

    async def recover(self, session) -> int:
        """После рестарта: восстановить режим тем активных игр (без создания новых).

        День: тема игры открыта живым, тема мафии закрыта.
        Ночь: тема игры закрыта, тема мафии открыта живой мафии.
        Мёртвые всегда блокированы серверной проверкой.
        """
        stmt = select(Game).where(
            Game.status.in_(ACTIVE_PHASES),
            (Game.game_thread_id.isnot(None)) | (Game.mafia_thread_id.isnot(None)),
        )
        games = list((await session.execute(stmt)).scalars().all())
        for game in games:
            if game.status == GameStatus.NIGHT.value:
                if game.game_chat_id and game.game_thread_id:
                    await self.gateway.close_topic(game.game_chat_id, game.game_thread_id)
                if game.mafia_chat_id and game.mafia_thread_id:
                    await self.gateway.reopen_topic(game.mafia_chat_id, game.mafia_thread_id)
            else:  # DAY / VOTING / STARTING
                if game.game_chat_id and game.game_thread_id:
                    await self.gateway.reopen_topic(game.game_chat_id, game.game_thread_id)
                if game.mafia_chat_id and game.mafia_thread_id:
                    await self.gateway.close_topic(game.mafia_chat_id, game.mafia_thread_id)
            logger.info(
                "Игра %s: режим тем восстановлен (фаза %s)", game.id, game.status
            )
        return len(games)

    # ------------------------------------------------------------ форумы

    async def _forums_for(
        self, session, group_id: int | None
    ) -> tuple[int | None, int | None]:
        """Пара форумов для игры с данной группой (через провайдер)."""
        getter = getattr(self.forums, "get_for", None)
        if getter is not None:
            return await getter(session, group_id)
        return await self.forums.get()  # legacy-провайдер без группы

    async def check_forums(self) -> dict:
        """Проверка подключения ГЛОБАЛЬНЫХ форумов — fallback игр без группы."""
        game_forum, mafia_forum = await self._forums_for(None, None)

        async def _check(chat_id):
            if not chat_id:
                return {"configured": False, "ok": False, "error": "не задан"}
            info = await self.gateway.chat_info(chat_id)
            if info is None:
                return {"configured": True, "ok": False, "error": "бот не имеет доступа"}
            if not info.get("is_forum"):
                return {
                    "configured": True, "ok": False, "title": info.get("title", ""),
                    "error": "чат не является форумом (нужна супергруппа с темами)",
                }
            return {"configured": True, "ok": True, "title": info.get("title", ""), "error": ""}

        return {
            "game": {"chat_id": game_forum, **await _check(game_forum)},
            "mafia": {"chat_id": mafia_forum, **await _check(mafia_forum)},
        }

    async def forums_overview(self) -> dict:
        """Глобальные форумы + per-group форумы всех групп (для /owner).

        Возвращает:
        {
          "global": {"game": {...}, "mafia": {...}},          # check_forums()
          "groups": [{"group_id", "title", "game": {...}, "mafia": {...}}, ...]
        }
        """
        from bot.database.models import GroupSettingsModel
        from bot.database.repositories.groups import GroupRepository

        async def _check(chat_id):
            if not chat_id:
                return {"configured": False, "ok": False, "error": "не задан"}
            info = await self.gateway.chat_info(chat_id)
            if info is None:
                return {"configured": True, "ok": False, "error": "бот не имеет доступа"}
            if not info.get("is_forum"):
                return {
                    "configured": True, "ok": False, "title": info.get("title", ""),
                    "error": "чат не является форумом (нужна супергруппа с темами)",
                }
            return {"configured": True, "ok": True, "title": info.get("title", ""), "error": ""}

        result: dict = {"global": await self.check_forums(), "groups": []}
        try:
            async with self.session_factory() as session:
                stmt = select(GroupSettingsModel).where(
                    (GroupSettingsModel.game_forum_chat_id.isnot(None))
                    | (GroupSettingsModel.mafia_forum_chat_id.isnot(None))
                )
                rows = list((await session.execute(stmt)).scalars().all())
                groups_repo = GroupRepository(session)
                for gs in rows:
                    group = await groups_repo.get(gs.group_id)
                    result["groups"].append({
                        "group_id": gs.group_id,
                        "title": group.title if group else f"Группа {gs.group_id}",
                        "game": {
                            "chat_id": gs.game_forum_chat_id,
                            **await _check(gs.game_forum_chat_id),
                        },
                        "mafia": {
                            "chat_id": gs.mafia_forum_chat_id,
                            **await _check(gs.mafia_forum_chat_id),
                        },
                    })
        except Exception:
            logger.warning("GameChat: не удалось собрать per-group форумы")
        return result

    # ------------------------------------------------------------ внутренние

    async def _notify_topics_failed(self, session, game: Game) -> None:
        """Тема партии не создана (обычно отозвано право управления темами).

        Сообщение в ОСНОВНУЮ группу игры — админам: что не так, что сделать,
        как проверить (/setup). Best-effort: сбой отправки не ломает игру.
        """
        if not game.group_id:
            return
        try:
            from bot.database.repositories.groups import GroupRepository

            group = await GroupRepository(session).get(game.group_id)
            if group is None:
                return
            await self.gateway.send(
                group.telegram_chat_id,
                "⚠️ <b>Mafia Online не удалось создать тему партии.</b>\n\n"
                "Вероятно, у бота нет права «Управление темами» в форумном чате.\n\n"
                "👉 Попросите администратора вернуть это право и повторите "
                "<code>/setup</code> в основной группе.\n\n"
                "Партия продолжается в личных чатах с ботом — ничего не потеряно.",
            )
        except Exception as exc:  # отправка не критична
            logger.warning(
                "Игра %s: не удалось предупредить группу о провале тем: %s",
                game.id, exc,
            )

    @staticmethod
    async def _room_name(session, game: Game) -> str | None:
        if not game.room_id:
            return None
        from bot.database.repositories.rooms import RoomRepository

        room = await RoomRepository(session).get(game.room_id)
        return room.name if room else None
