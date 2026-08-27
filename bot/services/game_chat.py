"""Игровые чаты партии: общий чат игры и ночной чат мафии.

АРХИТЕКТУРА (4 уровня, не смешиваются):
- 🏠 основная группа — обычное сообщество, без игровых кнопок/анонсов;
- 🎮 Game Chat (game_chat_id) — чат конкретной партии: общение живых игроков
  днём, анонсы фаз от бота, закрыт ночью;
- 🌙 Mafia Chat (mafia_chat_id) — чат конкретной партии: общение ЖИВОЙ мафии
  ночью, закрыт днём и для всех остальных;
- 🤖 ЛС с ботом — роли, ночные действия, дневное голосование (уже существует).

ОГРАНИЧЕНИЯ TELEGRAM BOT API (проверено, фиктивного кода нет):
- бот НЕ может создавать группы/каналы (нет метода createChat) — чаты создаёт
  игрок и добавляет бота администратором;
- бот НЕ может добавлять участников в чат — вместо этого бот создаёт
  инвайт-ссылки (createChatInviteLink) и рассылает их в ЛС игрокам, те
  вступают сами;
- бот МОЖЕТ: менять title (setChatTitle), ограничивать/разрешать отправку
  сообщений конкретным участникам (restrictChatMember), удалять сообщения
  (deleteMessage), отправлять анонсы (sendMessage);
- бот НЕ может удалить чат — по завершении игры права возвращаются, чаты
  сохраняются как история.

phase_manager остаётся источником истины: сервис только подписывается на его
переходы (on_night_started/on_day_started/on_death/on_game_ended/recover).
Все Telegram-операции best-effort: сбой в чатах НЕ ломает игровой поток.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Game, GameStatus, PlayerStatus
from bot.database.repositories.games import GamePlayerRepository
from bot.roles import Team, team_of
from bot.services.notifier import Notifier
from bot.utils.helpers import esc

logger = logging.getLogger(__name__)

ACTIVE_PHASES = (
    GameStatus.STARTING.value,
    GameStatus.NIGHT.value,
    GameStatus.DAY.value,
    GameStatus.VOTING.value,
)

# фазы, когда в общем чате можно писать живым (день и голосование)
DAY_TALK_PHASES = (GameStatus.DAY.value, GameStatus.VOTING.value)


# ------------------------------------------------------------------ gateway


class GameChatGateway(Protocol):
    """Тонкая обёртка над Telegram Bot API (для тестов — фейк)."""

    async def set_title(self, chat_id: int, title: str) -> bool: ...

    async def restrict(self, chat_id: int, user_id: int) -> bool:
        """Запретить отправку сообщений участнику (навсегда)."""
        ...

    async def unrestrict(self, chat_id: int, user_id: int) -> bool: ...

    async def invite_link(self, chat_id: int, name: str) -> str | None: ...

    async def send(self, chat_id: int, text: str) -> bool: ...

    async def delete_message(self, chat_id: int, message_id: int) -> bool: ...


class TelegramGameChatGateway:
    """Реальный Telegram Bot API. Каждая операция — best-effort.

    Требуемые права бота-админа: change_info (title), restrict_members,
    invite_users (ссылки), delete_messages (модерация).
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _safe(self, coro_factory, what: str) -> bool:
        try:
            await coro_factory()
            return True
        except Exception as exc:  # TelegramAPIError и любые другие
            logger.warning("GameChat: не удалось %s: %s", what, exc)
            return False

    async def set_title(self, chat_id: int, title: str) -> bool:
        return await self._safe(
            lambda: self.bot.set_chat_title(chat_id=chat_id, title=title[:128]),
            f"set_title({chat_id})",
        )

    async def restrict(self, chat_id: int, user_id: int) -> bool:
        from aiogram.types import ChatPermissions

        return await self._safe(
            lambda: self.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                use_independent_chat_permissions=True,
            ),
            f"restrict({chat_id}, {user_id})",
        )

    async def unrestrict(self, chat_id: int, user_id: int) -> bool:
        from aiogram.types import ChatPermissions

        return await self._safe(
            lambda: self.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_other_messages=True,
                    can_send_polls=True,
                    can_add_web_page_previews=True,
                ),
                use_independent_chat_permissions=True,
            ),
            f"unrestrict({chat_id}, {user_id})",
        )

    async def invite_link(self, chat_id: int, name: str) -> str | None:
        link_holder: dict[str, str] = {}

        async def _create() -> None:
            result = await self.bot.create_chat_invite_link(
                chat_id=chat_id, name=name[:32]
            )
            link_holder["link"] = result.invite_link

        ok = await self._safe(_create, f"invite_link({chat_id})")
        return link_holder.get("link") if ok else None

    async def send(self, chat_id: int, text: str) -> bool:
        return await self._safe(
            lambda: self.bot.send_message(
                chat_id=chat_id, text=text, disable_web_page_preview=True
            ),
            f"send({chat_id})",
        )

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return await self._safe(
            lambda: self.bot.delete_message(chat_id, message_id),
            f"delete_message({chat_id}, {message_id})",
        )


class NoopGameChatGateway:
    """Заглушка: чаты не привязаны/бот недоступен — все операции тихо пропускаются."""

    async def set_title(self, chat_id: int, title: str) -> bool:
        return False

    async def restrict(self, chat_id: int, user_id: int) -> bool:
        return False

    async def unrestrict(self, chat_id: int, user_id: int) -> bool:
        return False

    async def invite_link(self, chat_id: int, name: str) -> str | None:
        return None

    async def send(self, chat_id: int, text: str) -> bool:
        return False

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return False


# ------------------------------------------------------------------ service


class GameChatService:
    """Управление Game Chat / Mafia Chat. Истина фаз — PhaseManager."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        gateway: GameChatGateway,
        notifier: Notifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.notifier = notifier

    # ------------------------------------------------------------ привязка

    async def link_chat(
        self, session, game: Game, chat_id: int, kind: str, actor_user_id: int,
        is_privileged: bool = False,
    ) -> tuple[bool, str]:
        """Привязать Telegram-чат (созданный игроком) к игре.

        kind: "game" | "mafia". Серверные проверки: игра активна, чат свободен,
        роли чатов не совпадают. Идемпотентно: повторная привязка того же чата
        к той же игре — синхронизация.
        """
        if kind not in ("game", "mafia"):
            return False, "Неизвестный тип чата."
        if game.status not in ACTIVE_PHASES:
            return False, f"Игра #{game.id} не активна (статус {game.status})."
        if not is_privileged and not await self.can_manage(session, game, actor_user_id):
            return False, "Привязать чат может только создатель игры или админ."

        other_field = "mafia_chat_id" if kind == "game" else "game_chat_id"
        if getattr(game, other_field) == chat_id:
            return False, "Этот чат уже привязан как другой тип для этой игры."

        # чат не должен принадлежать ДРУГОЙ активной игре
        stmt = (
            select(Game.id)
            .where(Game.status.in_(ACTIVE_PHASES), Game.id != game.id)
            .where((Game.game_chat_id == chat_id) | (Game.mafia_chat_id == chat_id))
        )
        busy = (await session.execute(stmt)).scalars().first()
        if busy:
            return False, f"Чат уже занят активной игрой #{busy}."

        field = "game_chat_id" if kind == "game" else "mafia_chat_id"
        setattr(game, field, chat_id)
        await session.flush()

        room_name = await self._room_name(session, game)
        title = self.chat_title(kind, game.id, room_name)
        await self.gateway.set_title(chat_id, title)
        logger.info("Игра %s: %s чат привязан (%s)", game.id, kind, chat_id)

        # полный онбординг: анонс текущей фазы + права + инвайты
        players = await GamePlayerRepository(session).list_for_game(game.id)
        if kind == "game":
            await self._announce_phase(session, game, players)
            await self._invite_players(session, game, players)
        else:
            await self._sync_mafia_chat(session, game, players)
            alive_mafia = [
                p for p in players
                if p.is_alive and team_of(p.role) == Team.MAFIA
            ]
            await self._invite_players(session, game, alive_mafia, mafia=True)
        await self._sync_permissions(session, game, players)
        return True, f"✅ Чат привязан к игре #{game.id} как {self.kind_label(kind)}."

    async def unlink_chats(self, session, game: Game) -> None:
        """Отвязать чаты (например, при завершении — только пометки не трогаем)."""
        # чаты сохраняются в истории игры: ничего не чистим
        return None

    @staticmethod
    def chat_title(kind: str, game_id: int, room_name: str | None) -> str:
        base = room_name.strip()[:24] if room_name and room_name.strip() else "Игра"
        return (f"🌙 Мафия — {base} #{game_id}" if kind == "mafia"
                else f"🎮 Мафия — {base} #{game_id}")

    @staticmethod
    def kind_label(kind: str) -> str:
        return "чат мафии" if kind == "mafia" else "игровой чат"

    async def can_manage(self, session, game: Game, user_id: int) -> bool:
        """Создатель партии (создатель комнаты) управляет чатами игры."""
        if not game.room_id:
            return False
        from bot.database.repositories.rooms import RoomRepository

        room = await RoomRepository(session).get(game.room_id)
        return room is not None and room.creator_id == user_id

    @staticmethod
    async def _room_name(session, game: Game) -> str | None:
        if not game.room_id:
            return None
        from bot.database.repositories.rooms import RoomRepository

        room = await RoomRepository(session).get(game.room_id)
        return room.name if room else None

    # ------------------------------------------------------------ события фаз

    async def on_night_started(self, session, game: Game, players) -> None:
        """DAY/VOTING -> NIGHT: закрыть общий чат, открыть чат мафии живым."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        night_no = game.day_number
        if game.game_chat_id:
            await self.gateway.send(
                game.game_chat_id,
                "🌙 <b>НАСТУПИЛА НОЧЬ</b>\n\n"
                f"Ночь {night_no}\n"
                "Игровой чат закрыт для общения.\n"
                "🔮 Ночные действия выполняются в личном чате с ботом.",
            )
        if game.mafia_chat_id:
            alive_mafia = [
                p for p in players
                if p.is_alive and team_of(p.role) == Team.MAFIA
            ]
            await self.gateway.send(
                game.mafia_chat_id,
                "🌙 <b>НОЧЬ МАФИИ</b>\n\n"
                "Обсуждайте жертву здесь. Выбор цели — в личном чате с ботом.",
            )
            for p in alive_mafia:
                await self.gateway.unrestrict(game.mafia_chat_id, p.user.telegram_id)
        await self._sync_permissions(session, game, players)
        logger.info("Игра %s: ночной режим чатов применён", game.id)

    async def on_day_started(self, session, game: Game, players) -> None:
        """NIGHT -> DAY: открыть общий чат живым, закрыть чат мафии."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        if game.game_chat_id:
            await self.gateway.send(
                game.game_chat_id,
                "☀️ <b>НАСТУПИЛ ДЕНЬ</b>\n\n"
                f"День {game.day_number}\n"
                "Живые игроки могут обсуждать происходящее.\n"
                "🗳 Голосование проходит в личном чате с ботом.",
            )
        if game.mafia_chat_id:
            await self.gateway.send(
                game.mafia_chat_id,
                "☀️ Наступил день — чат мафии закрыт до следующей ночи.",
            )
        await self._sync_permissions(session, game, players)
        logger.info("Игра %s: дневной режим чатов применён", game.id)

    async def on_death(self, session, game: Game, gp) -> None:
        """Смерть игрока: немедленно закрыть ему отправку в обоих чатах."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        tg_id = gp.user.telegram_id
        if game.game_chat_id:
            await self.gateway.restrict(game.game_chat_id, tg_id)
        if game.mafia_chat_id and team_of(gp.role) == Team.MAFIA:
            await self.gateway.restrict(game.mafia_chat_id, tg_id)
        logger.info("Игра %s: игрок %s умер — чаты закрыты для него", game.id, gp.user_id)

    async def on_game_ended(self, session, game: Game, players, title: str) -> None:
        """🏁 Финал: анонс в общий чат и возврат обычных прав всем участникам."""
        if not (game.game_chat_id or game.mafia_chat_id):
            return
        if game.game_chat_id:
            winners = getattr(game, "winner", None) or "—"
            await self.gateway.send(
                game.game_chat_id,
                f"🏁 <b>ИГРА ЗАВЕРШЕНА!</b>\n\n🏆 Победили: {esc(title or winners)}\n\n"
                "Игровые ограничения сняты. Чат сохранён как история партии.\n"
                "Подробности — в личке у бота: /history",
            )
        if game.mafia_chat_id:
            await self.gateway.send(
                game.mafia_chat_id,
                "🏁 Игра завершена. Чат мафии закрыт, история сохранена.",
            )
        # вернуть обычные права всем участникам в обоих чатах
        for gp in players:
            if gp.status == PlayerStatus.SPECTATOR.value:
                continue
            tg_id = gp.user.telegram_id
            if game.game_chat_id:
                await self.gateway.unrestrict(game.game_chat_id, tg_id)
            if game.mafia_chat_id:
                await self.gateway.unrestrict(game.mafia_chat_id, tg_id)
        logger.info("Игра %s: игровые чаты раскрыты (конец игры)", game.id)

    # ------------------------------------------------------------ модерация

    async def chat_kind(self, session, chat_id: int) -> str | None:
        """Какой активной игре принадлежит чат: 'game' | 'mafia' | None."""
        stmt = select(Game).where(
            Game.status.in_(ACTIVE_PHASES),
            (Game.game_chat_id == chat_id) | (Game.mafia_chat_id == chat_id),
        )
        game = (await session.execute(stmt)).scalars().first()
        if game is None:
            return None
        if game.game_chat_id == chat_id:
            return "game"
        return "mafia"

    async def enforce_message(
        self, session, chat_id: int, user, message_id: int, *, is_command: bool = False,
    ) -> bool:
        """Серверная проверка сообщения в игровом чате (ТЗ-25).

        Возвращает True, если сообщение обработано (удалено) — хендлеры дальше
        не нужны. Проверки: чат принадлежит АКТИВНОЙ игре ровно один раз,
        отправитель — участник этой игры, жив, и фаза позволяет писать.
        """
        kind = await self.chat_kind(session, chat_id)
        if kind is None:
            return False  # не игровой чат — обычная обработка
        game = await self._game_by_chat(session, chat_id)
        gp = await GamePlayerRepository(session).get_by_user(game.id, user.id)

        allowed = False
        if kind == "mafia":
            allowed = (
                game.status == GameStatus.NIGHT.value
                and gp is not None
                and gp.is_alive
                and team_of(gp.role) == Team.MAFIA
            )
        else:  # общий игровой чат
            if gp is not None and gp.is_alive and game.status in DAY_TALK_PHASES:
                allowed = True
            elif is_command and gp is not None and await self.can_manage(
                session, game, user.id
            ):
                allowed = True  # команды управления чатами — создателю игры

        if allowed:
            return False
        await self.gateway.delete_message(chat_id, message_id)
        logger.info(
            "GameChat: удалено сообщение %s в %s-чате игры %s (user %s)",
            message_id, kind, game.id, user.id,
        )
        return True

    # ------------------------------------------------------------ recover

    async def recover(self, session) -> int:
        """После рестарта бота: синхронизировать права чатов активных игр."""
        stmt = select(Game).where(
            Game.status.in_(ACTIVE_PHASES),
            (Game.game_chat_id.isnot(None)) | (Game.mafia_chat_id.isnot(None)),
        )
        games = list((await session.execute(stmt)).scalars().all())
        for game in games:
            players = await GamePlayerRepository(session).list_for_game(game.id)
            await self._sync_permissions(session, game, players)
            logger.info(
                "Игра %s: права чатов восстановлены (фаза %s)", game.id, game.status
            )
        return len(games)

    # ------------------------------------------------------------ внутренние

    async def _game_by_chat(self, session, chat_id: int) -> Game | None:
        stmt = select(Game).where(
            Game.status.in_(ACTIVE_PHASES),
            (Game.game_chat_id == chat_id) | (Game.mafia_chat_id == chat_id),
        )
        return (await session.execute(stmt)).scalars().first()

    async def _announce_phase(self, session, game: Game, players) -> None:
        """Анонс текущей фазы при привязке чата (игра уже идёт)."""
        if not game.game_chat_id:
            return
        if game.status == GameStatus.NIGHT.value:
            await self.gateway.send(
                game.game_chat_id,
                "🎮 <b>Чат партии привязан.</b>\n\n"
                f"Сейчас ночь {game.day_number}. Общение в этом чате закрыто "
                "до утра; действия — в личном чате с ботом.",
            )
        elif game.status in DAY_TALK_PHASES:
            await self.gateway.send(
                game.game_chat_id,
                "🎮 <b>Чат партии привязан.</b>\n\n"
                f"Сейчас день {game.day_number}. Обсуждайте! "
                "Голосование — в личном чате с ботом.",
            )
        else:  # STARTING
            await self.gateway.send(
                game.game_chat_id, "🎮 <b>Чат партии привязан.</b> Игра начинается…"
            )

    async def _invite_players(
        self, session, game: Game, players, *, mafia: bool = False
    ) -> None:
        """Инвайт-ссылка в ЛС: боты не могут добавить участников в чат сами."""
        if self.notifier is None:
            return
        chat_id = game.mafia_chat_id if mafia else game.game_chat_id
        if chat_id is None or not players:
            return
        link = await self.gateway.invite_link(
            chat_id, f"Игра #{game.id}" + (" (мафия)" if mafia else "")
        )
        if not link:
            return
        label = "🌙 Чат мафии вашей партии" if mafia else "🎮 Игровой чат партии"
        for gp in players:
            if gp.status == PlayerStatus.SPECTATOR.value:
                continue
            await self.notifier.send(
                gp.user.telegram_id,
                f"{label} (Игра #{game.id}):\n{link}\n\n"
                + ("Вступить нужно до наступления ночи." if mafia
                   else "Вступайте, чтобы обсуждать игру."),
            )

    async def _sync_mafia_chat(self, session, game: Game, players) -> None:
        """Чат мафии: доступ только живым мафиози и только ночью."""
        if not game.mafia_chat_id:
            return
        for p in players:
            if p.status == PlayerStatus.SPECTATOR.value:
                continue
            is_alive_mafia = (
                p.is_alive and team_of(p.role) == Team.MAFIA
                and game.status == GameStatus.NIGHT.value
            )
            if is_alive_mafia:
                await self.gateway.unrestrict(game.mafia_chat_id, p.user.telegram_id)
            else:
                await self.gateway.restrict(game.mafia_chat_id, p.user.telegram_id)

    async def _sync_permissions(self, session, game: Game, players) -> None:
        """Синхронизировать права обоих чатов с текущей фазой и жизнью."""
        for p in players:
            if p.status == PlayerStatus.SPECTATOR.value:
                continue
            tg_id = p.user.telegram_id
            if game.game_chat_id:
                may_talk = p.is_alive and game.status in DAY_TALK_PHASES
                if may_talk:
                    await self.gateway.unrestrict(game.game_chat_id, tg_id)
                else:
                    await self.gateway.restrict(game.game_chat_id, tg_id)
        await self._sync_mafia_chat(session, game, players)
