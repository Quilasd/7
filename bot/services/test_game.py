"""DEBUG/TEST MODE: тестовые игры с ботами-игроками для локальной отладки.

Возможности (доступны только админам, см. handlers/testgame.py):
- создание игры «реальный админ + N ботов TestPlayerN»;
- автоматические ночные действия ботов (по правилам их ролей);
- автоматическое голосование ботов;
- супервизор-цикл, который действует за ботов каждую фазу;
- мгновенный пропуск фазы (ускорение таймеров);
- дамп состояния игры в консоль (в тестовом режиме роли видны);
- принудительное завершение.

Гарантии изоляции от обычных игр:
- у тестовых игр games.settings["test_mode"] = True; все методы сервиса
  проверяют этот флаг и работают только с тестовыми играми;
- тестовые игры не влияют на рейтинг/статистику (phase_manager это учитывает);
- боты — это пользователи с is_test=True и отрицательным telegram_id
  (реальные Telegram ID пользователей отрицательными не бывают);
- боты исключены из рейтинга и рассылок (repositories/users.py);
- комнаты тестовых игр приватные — их нет в общем списке.
"""

from __future__ import annotations

import asyncio
import logging
import random

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Game, GameStatus, Room, RoomPlayer, RoomStatus, User
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.roles import ActionType, get_role, team_of
from bot.services.game_manager import GameManager
from bot.services.notifier import Notifier
from bot.services.phase_manager import PhaseManager
from bot.utils.helpers import deadline_in, display_name

logger = logging.getLogger(__name__)

# Отрицательные ID для ботов: реальный Telegram ID пользователя >= 0
BOT_ID_BASE = -900_000_000

PHASE_NAMES_RU = {
    GameStatus.STARTING.value: "⏳ Подготовка",
    GameStatus.NIGHT.value: "🌙 Ночь",
    GameStatus.DAY.value: "☀️ День",
    GameStatus.VOTING.value: "🗳 Голосование",
    GameStatus.ENDED.value: "🏁 Завершена",
}


class TestGameManager:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        games: GameManager,
        phases: PhaseManager,
        notifier: Notifier,
    ) -> None:
        self.session_factory = session_factory
        self.games = games
        self.phases = phases
        self.notifier = notifier
        # game_id -> {"task": asyncio.Task, "auto": bool, "include_admin": bool}
        self._supervisors: dict[int, dict] = {}

    # ------------------------------------------------------------- создание

    async def create_test_game(
        self,
        admin_user_id: int,
        players_count: int,
        supervisor_interval: float = 1.0,
        auto_include_admin: bool = False,
    ) -> tuple[int | None, str]:
        """Создаёт игру: 1 реальный админ + (players_count-1) ботов.

        Тайминги ускоренные (по 6 секунд на фазу), старт через 2 секунды.
        """
        if not (4 <= players_count <= 8):
            return None, "Игроков должно быть от 4 до 8."

        bots_count = players_count - 1
        mafia = 2 if players_count >= 7 else 1
        settings = {
            "roles": {"mafia": mafia, "detective": 1, "doctor": 1},
            "night_seconds": 6,
            "day_seconds": 6,
            "vote_seconds": 6,
            "start_countdown_seconds": 2,
            "tie_rule": "revote",
            "reveal_roles_on_death": True,
            "test_mode": True,
        }

        async with self.session_factory() as session:
            admin = await UserRepository(session).get_by_id(admin_user_id)
            if admin is None:
                return None, "Администратор не найден в БД."
            if await GamePlayerRepository(session).active_game_of_user(admin.id):
                return None, "У тебя уже есть активная игра — заверши её сначала."

            users_repo = UserRepository(session)
            bots: list[User] = []
            for index in range(1, bots_count + 1):
                telegram_id = BOT_ID_BASE - admin.id * 100 - index
                bot_user = await users_repo.get_by_telegram_id(telegram_id)
                if bot_user is None:
                    bot_user = User(
                        telegram_id=telegram_id,
                        username=None,
                        display_name=f"TestPlayer{index}",
                        is_test=True,
                    )
                    session.add(bot_user)
                    await session.flush()
                else:
                    bot_user.display_name = f"TestPlayer{index}"
                    bot_user.is_test = True
                bots.append(bot_user)

            room = Room(
                creator_id=admin.id,
                name=f"🧪 Тест {admin.id}",
                max_players=players_count,
                min_players=4,
                is_private=True,  # не попадает в общий список комнат
                status=RoomStatus.OPEN.value,
                settings=settings,
            )
            session.add(room)
            await session.flush()
            session.add(RoomPlayer(room_id=room.id, user_id=admin.id, is_ready=True))
            for bot_user in bots:
                session.add(RoomPlayer(room_id=room.id, user_id=bot_user.id, is_ready=True))
            await session.commit()
            room_id = room.id

        result = await self.games.start_game_from_room(room_id, admin_user_id)
        if not result.ok:
            # комната остаётся открытой — закрываем, чтобы не мешала
            async with self.session_factory() as session:
                stale = await RoomRepository(session).get(room_id)
                if stale and stale.status == RoomStatus.OPEN.value:
                    stale.status = RoomStatus.CLOSED.value
                    await session.commit()
            return None, f"Не удалось запустить тестовую игру: {result.message}"

        async with self.session_factory() as session:
            game_id = (await RoomRepository(session).get(room_id)).game_id
        self.start_supervisor(game_id, interval=supervisor_interval, include_admin=auto_include_admin)
        logger.info(
            "ТЕСТ-РЕЖИМ: создана игра %s (%s игроков, админ=%s)", game_id, players_count, admin_user_id
        )
        await self.dump_state(game_id)
        return game_id, f"🧪 Тестовая игра #{game_id} создана: {players_count} участников."

    # ---------------------------------------------------- действия за ботов

    def _is_test_game(self, game: Game | None) -> bool:
        return bool(game is not None and game.get_setting("test_mode"))

    async def auto_night_actions(self, game_id: int, include_admin: bool = False) -> int:
        """Боты с ночными ролями выполняют допустимые действия.

        Возвращает количество выполненных действий. Детерминированный RNG
        (seed = game_id*7919 + день) делает поведение воспроизводимым.
        """
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if not self._is_test_game(game) or game.status != GameStatus.NIGHT.value:
                return 0
            players = await GamePlayerRepository(session).list_for_game(game_id)
            day_number = game.day_number

        rng = random.Random(game_id * 7919 + day_number)
        submitted = 0
        for gp in players:
            if not gp.is_alive:
                continue
            if not gp.user.is_test and not include_admin:
                continue  # за админа играем вручную
            role = get_role(gp.role)
            if role is None or role.night_action is None:
                continue
            candidates = [p for p in players if p.is_alive and p.user_id != gp.user_id]
            if role.night_action == ActionType.KILL:
                candidates = [p for p in candidates if team_of(p.role) != role.team]
            rng.shuffle(candidates)
            for target in candidates:
                result = await self.games.submit_night_action(
                    game_id, gp.user_id, role.night_action.value, target.user_id
                )
                if result.ok:
                    submitted += 1
                    logger.debug(
                        "ТЕСТ-РЕЖИМ: игра %s, бот %s (%s) -> %s",
                        game_id, gp.user_id, role.id, target.user_id,
                    )
                    break
        return submitted

    async def auto_vote(self, game_id: int, include_admin: bool = False) -> int:
        """Живые боты голосуют за случайного допустимого кандидата."""
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if not self._is_test_game(game) or game.status != GameStatus.VOTING.value:
                return 0
            players = await GamePlayerRepository(session).list_for_game(game_id)
            from bot.services.vote_manager import VoteManager

            round_no = VoteManager.current_round(game)
            candidates = VoteManager.candidates(game)
            alive = [p for p in players if p.is_alive]
            allowed_ids = set(candidates) if candidates is not None else {p.user_id for p in alive}

        rng = random.Random(game_id * 104729 + game.day_number * 100 + round_no)
        submitted = 0
        for gp in alive:
            if not gp.user.is_test and not include_admin:
                continue
            options = [uid for uid in allowed_ids if uid != gp.user_id]
            if not options:
                continue
            rng.shuffle(options)
            for target_id in options:
                result = await self.games.cast_vote(game_id, gp.user_id, target_id)
                if result.ok:
                    submitted += 1
                    break
        return submitted

    # ------------------------------------------------------------- ускорение

    async def skip_phase(self, game_id: int) -> str:
        """Мгновенно завершает текущую фазу (как будто таймер сгорел)."""
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if not self._is_test_game(game):
                return "Это не тестовая игра."
            status = game.status
        handlers = {
            GameStatus.STARTING.value: self.phases.begin_game,
            GameStatus.NIGHT.value: self.phases.end_night,
            GameStatus.DAY.value: self.phases.begin_voting,
            GameStatus.VOTING.value: self.phases.end_voting,
        }
        callback = handlers.get(status)
        if callback is None:
            return f"Фаза {status} не пропускается."
        await callback(game_id)
        logger.info("ТЕСТ-РЕЖИМ: игра %s, фаза %s пропущена", game_id, status)
        new_status = await self._status_of(game_id)
        return f"⏩ Фаза {PHASE_NAMES_RU.get(status, status)} пропущена. Теперь: {PHASE_NAMES_RU.get(new_status, new_status)}"

    async def _status_of(self, game_id: int) -> str:
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            return game.status if game else GameStatus.ENDED.value

    # ------------------------------------------------------------- завершение

    async def finish(self, game_id: int) -> str:
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if not self._is_test_game(game):
                return "Это не тестовая игра."
        done = await self.phases.force_end(game_id, "Тестовая игра завершена администратором")
        self.stop_supervisor(game_id)
        return "🏁 Тестовая игра завершена." if done else "Игра уже завершена."

    # ---------------------------------------------------------- супервизор

    def start_supervisor(self, game_id: int, interval: float = 1.0, include_admin: bool = False) -> None:
        """Фоновый цикл: в NIGHT выполняет ночные действия ботов,
        в VOTING — голосует. Фазы не трогает (ими управляют таймеры/админ)."""
        self.stop_supervisor(game_id)
        entry: dict = {"auto": True, "include_admin": include_admin}
        entry["task"] = asyncio.create_task(
            self._supervise(game_id, entry, interval), name=f"test-supervisor-{game_id}"
        )
        self._supervisors[game_id] = entry

    async def _supervise(self, game_id: int, entry: dict, interval: float) -> None:
        acted_nights: set[int] = set()
        voted_rounds: set[tuple[int, int]] = set()
        try:
            while True:
                async with self.session_factory() as session:
                    game = await GameRepository(session).get(game_id)
                    if game is None or game.status == GameStatus.ENDED.value:
                        break
                    if not self._is_test_game(game):
                        logger.warning("ТЕСТ-РЕЖИМ: игра %s не тестовая — супервизор остановлен", game_id)
                        break
                    status, day, round_no = (
                        game.status,
                        game.day_number,
                        int((game.vote_context or {}).get("round_no", 1)),
                    )
                if entry["auto"]:
                    if status == GameStatus.NIGHT.value and day not in acted_nights:
                        count = await self.auto_night_actions(game_id, entry["include_admin"])
                        acted_nights.add(day)
                        if count:
                            logger.info("ТЕСТ-РЕЖИМ: игра %s, ночь %s — боты выполнили %s действий", game_id, day, count)
                    elif status == GameStatus.VOTING.value and (day, round_no) not in voted_rounds:
                        count = await self.auto_vote(game_id, entry["include_admin"])
                        voted_rounds.add((day, round_no))
                        if count:
                            logger.info("ТЕСТ-РЕЖИМ: игра %s, день %s круг %s — проголосовало ботов: %s", game_id, day, round_no, count)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        finally:
            self._supervisors.pop(game_id, None)

    def toggle_auto(self, game_id: int) -> bool | None:
        """Вкл/выкл авто-действий ботов. None — супервизор не запущен."""
        entry = self._supervisors.get(game_id)
        if entry is None:
            return None
        entry["auto"] = not entry["auto"]
        return entry["auto"]

    def auto_is_on(self, game_id: int) -> bool:
        entry = self._supervisors.get(game_id)
        return bool(entry and entry["auto"])

    def stop_supervisor(self, game_id: int) -> None:
        entry = self._supervisors.pop(game_id, None)
        if entry and entry.get("task") and not entry["task"].done():
            entry["task"].cancel()

    def stop_all(self) -> None:
        for game_id in list(self._supervisors):
            self.stop_supervisor(game_id)

    def supervised_games(self) -> list[int]:
        return sorted(self._supervisors.keys())

    # ------------------------------------------------------------ дамп

    async def act_now(self, game_id: int) -> str:
        """Выполнить действия ботов прямо сейчас (независимо от супервизора)."""
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if not self._is_test_game(game):
                return "Это не тестовая игра."
            status = game.status
        if status == GameStatus.NIGHT.value:
            count = await self.auto_night_actions(game_id, include_admin=True)
            return f"🤖 Ночные действия выполнены: {count}."
        if status == GameStatus.VOTING.value:
            count = await self.auto_vote(game_id, include_admin=True)
            return f"🤖 Проголосовали боты: {count}."
        return f"Сейчас фаза {PHASE_NAMES_RU.get(status, status)} — авто-действий нет."

    async def dump_state(self, game_id: int) -> str:
        """Полное состояние игры: роли, живые, действия, голоса, дедлайн.

        Выводит в консоль (logger.info) и возвращает текстом для чата.
        Роли раскрываются — это отладочный режим.
        """
        from bot.database.repositories.actions import GameActionRepository
        from bot.database.repositories.votes import VoteRepository

        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if game is None:
                return "Игра не найдена."
            players = await GamePlayerRepository(session).list_for_game(game_id)
            actions = await GameActionRepository(session).night_actions(game_id, game.day_number)
            votes = await VoteRepository(session).round_votes(
                game_id, game.day_number, int((game.vote_context or {}).get("round_no", 1))
            )

        name_by_id = {p.user_id: display_name(p.user) for p in players}
        lines = [
            f"🧪 <b>ТЕСТ-ИГРА #{game.id}</b>",
            f"Фаза: <b>{PHASE_NAMES_RU.get(game.status, game.status)}</b> · день {game.day_number}"
            + (f" · до конца фазы {deadline_in(game.phase_deadline)}с" if game.phase_deadline else ""),
            "",
            "👥 Игроки:",
        ]
        for gp in players:
            role = get_role(gp.role)
            mark = "🟢" if gp.is_alive else "💀"
            bot_mark = "🤖" if gp.user.is_test else "👤"
            cause = f" ({gp.death_cause})" if gp.death_cause else ""
            lines.append(
                f"{mark} {bot_mark} {display_name(gp.user)} — {role.title if role else '—'}{cause}"
            )
        if actions:
            lines += ["", "🌙 Действия ночи:"]
            for action in actions:
                verb = {
                    "kill": "убить", "heal": "лечить", "check": "проверить",
                    "block": "блокировать", "protect": "защищать",
                }.get(action.action_type, action.action_type)
                lines.append(
                    f"• {name_by_id.get(action.actor_id, action.actor_id)} → {verb} → "
                    f"{name_by_id.get(action.target_id, action.target_id)}"
                )
        if votes and game.status == GameStatus.VOTING.value:
            lines += ["", "🗳 Голоса:"]
            for vote in votes:
                lines.append(
                    f"• {name_by_id.get(vote.voter_id, vote.voter_id)} → {name_by_id.get(vote.target_id, vote.target_id)}"
                )
        if game.status == GameStatus.ENDED.value:
            lines += ["", f"🏁 Победитель: <b>{game.winner}</b>"]

        text = "\n".join(lines)
        # Консольный вывод (без HTML-тегов)
        import re

        console = re.sub(r"<[^>]+>", "", text)
        logger.info("Состояние тест-игры %s:\n%s", game_id, console)
        return text
