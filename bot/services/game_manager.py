"""GameManager: старт игры, приём действий игроков, выход из игры.

Все методы открывают собственную сессию и берут лок игры — пользовательские
действия сериализуются с фазовыми переходами, поэтому «действие после смерти»
или «голосование ночью» невозможны при любой гонке нажатий.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Game, GamePlayer, GameStatus, PlayerStatus, RoomStatus
from bot.database.repositories.actions import GameActionRepository
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.roles import ActionType, get_role, team_of
from bot.services import game_view
from bot.services.notifier import Notifier
from bot.services.phase_manager import ACTIVE_PHASES, GameLocks, PhaseManager
from bot.services.role_manager import distribute_roles, evaluate_win, validate_setup
from bot.services.vote_manager import VoteManager
from bot.utils.helpers import display_name, esc, future, utcnow

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    message: str


class GameManager:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        notifier: Notifier,
        phases: PhaseManager,
        locks: GameLocks,
    ) -> None:
        self.session_factory = session_factory
        self.notifier = notifier
        self.phases = phases
        self.locks = locks

    # ------------------------------------------------------------ старт игры

    async def start_game_from_room(self, room_id: int, creator_user_id: int) -> ActionResult:
        """Создание игры из комнаты: распределение ролей + рассылка карт ролей."""
        async with self.session_factory() as session:
            rooms = RoomRepository(session)
            room = await rooms.get(room_id)
            if room is None:
                return ActionResult(False, "Комната не найдена.")
            if room.status != RoomStatus.OPEN.value:
                return ActionResult(False, "Игра в этой комнате уже начата или закрыта.")
            if room.creator_id != creator_user_id:
                return ActionResult(False, "Запустить игру может только создатель комнаты.")

            memberships = room.players  # selectin
            if len(memberships) < room.min_players:
                return ActionResult(
                    False, f"Мало игроков: {len(memberships)}/{room.min_players}."
                )
            not_ready = [m for m in memberships if not m.is_ready]
            if not_ready:
                return ActionResult(
                    False,
                    "Не все игроки готовы: "
                    + ", ".join(esc(display_name(m.user)) for m in not_ready),
                )

            settings = dict(room.settings or {})
            setup = dict(settings.get("roles", {}))
            errors = validate_setup(setup, room.max_players, room.min_players)
            if errors:
                return ActionResult(False, "Настройки ролей некорректны: " + "; ".join(errors))
            if sum(c for c in setup.values() if c > 0) > len(memberships):
                # ролей больше, чем игроков — недопустимо
                return ActionResult(False, "Ролей больше, чем игроков. Измените набор ролей.")

            user_ids = [m.user_id for m in memberships]
            distribution = distribute_roles(user_ids, setup)

            countdown = int(settings.get("start_countdown_seconds", 5))
            game = Game(
                room_id=room.id,
                status=GameStatus.STARTING.value,
                max_players=room.max_players,
                day_number=0,
                settings=settings,
                phase_deadline=future(countdown),
                started_at=utcnow(),
            )
            session.add(game)
            await session.flush()

            for slot, user_id in enumerate(user_ids, start=1):
                session.add(
                    GamePlayer(
                        game_id=game.id,
                        user_id=user_id,
                        role=distribution.roles_by_user[user_id],
                        status=PlayerStatus.ALIVE.value,
                        is_alive=True,
                        slot=slot,
                    )
                )
            room.status = RoomStatus.PLAYING.value
            room.game_id = game.id
            await session.commit()

            players = await GamePlayerRepository(session).list_for_game(game.id)
            await self._send_role_cards(players)
            self.phases.timers.schedule(
                game.id, "starting", countdown, self._make_begin(game.id)
            )
            logger.info(
                "Игра %s создана из комнаты %s: %s игроков", game.id, room.id, len(players)
            )
            return ActionResult(
                True,
                f"🎮 Игра #{game.id} запущена! Роли распределены, первая ночь через {countdown} сек.",
            )

    def _make_begin(self, game_id: int):
        async def _run() -> None:
            await self.phases.begin_game(game_id)
        return _run

    async def _send_role_cards(self, players: list[GamePlayer]) -> None:
        """Личная рассылка карт ролей. Мафии — список союзников."""
        role_groups: dict[str, list[GamePlayer]] = {}
        for gp in players:
            role_groups.setdefault(gp.role or "", []).append(gp)

        for gp in players:
            role = get_role(gp.role)
            teammates = None
            if role and role.knows_teammates:
                mate_users = [
                    mate.user
                    for mate in role_groups.get(gp.role or "", [])
                    if mate.user_id != gp.user_id
                ]
                teammates = mate_users or None
            card = game_view.role_card(gp, teammates)
            await self.notifier.send(gp.user.telegram_id, card)

    # ------------------------------------------------------ ночные действия

    async def submit_night_action(
        self, game_id: int, actor_user_id: int, action_type: str, target_user_id: int
    ) -> ActionResult:
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                games = GameRepository(session)
                game = await games.get(game_id)
                if game is None:
                    return ActionResult(False, "Игра не найдена.")
                if game.status != GameStatus.NIGHT.value:
                    return ActionResult(False, "Сейчас не ночь — действие невозможно.")
                if game.winner:
                    return ActionResult(False, "Игра уже завершена.")

                players_repo = GamePlayerRepository(session)
                actor = await players_repo.get_by_user(game.id, actor_user_id)
                if actor is None or actor.status == PlayerStatus.SPECTATOR.value:
                    return ActionResult(False, "Ты не участник этой игры.")
                if not actor.is_alive:
                    return ActionResult(False, "💀 Мёртвые не действуют.")

                role = get_role(actor.role)
                if role is None or role.night_action is None:
                    return ActionResult(False, "У твоей роли нет ночных действий.")
                if role.night_action.value != action_type:
                    return ActionResult(False, "Это действие не соответствует твоей роли.")

                target = await players_repo.get_by_user(game.id, target_user_id)
                if target is None or not target.is_alive:
                    return ActionResult(False, "Цель недоступна (мертва или не в игре).")
                if target.user_id == actor.user_id and not role.can_target_self:
                    return ActionResult(False, "Нельзя выбрать себя.")
                if role.night_action == ActionType.KILL and team_of(target.role) == role.team:
                    return ActionResult(False, "Нельзя выбрать союзника.")
                if role.no_repeat_target and game.day_number > 1:
                    actions = GameActionRepository(session)
                    prev = await actions.get_action(
                        game.id, actor.user_id, action_type, game.day_number - 1
                    )
                    if prev and prev.target_id == target.user_id:
                        return ActionResult(
                            False, "Нельзя выбирать одну и ту же цель две ночи подряд."
                        )

                actions = GameActionRepository(session)
                await actions.upsert(
                    game_id=game.id,
                    actor_id=actor.user_id,
                    target_id=target.user_id,
                    action_type=action_type,
                    day_number=game.day_number,
                )
                await session.commit()

                target_name = esc(display_name(target.user))
                note = ""
                if role.team.value == "mafia":
                    note = "\n\nℹ️ Финальную жертву выберет большинство мафии."
                logger.info(
                    "Игра %s: действие %s от игрока %s (день %s)",
                    game.id, action_type, actor.user_id, game.day_number,
                )
                return ActionResult(
                    True,
                    f"✅ Действие принято.\n\n{role.emoji} {role.action_verb}: <b>{target_name}</b>."
                    f"\nМожно изменить выбор до конца ночи.{note}",
                )

    # --------------------------------------------------------- голосование

    async def cast_vote(
        self, game_id: int, voter_user_id: int, target_user_id: int
    ) -> ActionResult:
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                games = GameRepository(session)
                game = await games.get(game_id)
                if game is None:
                    return ActionResult(False, "Игра не найдена.")
                if game.status != GameStatus.VOTING.value:
                    return ActionResult(False, "Сейчас не фаза голосования.")

                players_repo = GamePlayerRepository(session)
                voter = await players_repo.get_by_user(game.id, voter_user_id)
                if voter is None or voter.status == PlayerStatus.SPECTATOR.value:
                    return ActionResult(False, "Ты не участник этой игры.")
                if not voter.is_alive:
                    return ActionResult(False, "💀 Мёртвые не голосуют.")

                if target_user_id == voter_user_id:
                    return ActionResult(False, "Голосовать за себя нельзя.")

                target = await players_repo.get_by_user(game.id, target_user_id)
                if target is None or not target.is_alive:
                    return ActionResult(False, "Этот игрок уже выбыл.")

                candidates = VoteManager.candidates(game)
                if candidates is not None and target.user_id not in candidates:
                    return ActionResult(False, "В этом круге голосуют только за лидеров прошлого круга.")

                vote_manager = VoteManager(session)
                existing = await vote_manager.get_vote_of(game, voter.user_id)
                await vote_manager.cast_vote(game, voter, target)
                await session.commit()

                target_name = esc(display_name(target.user))
                changed = "Голос изменён" if existing else "Голос учтён"
                return ActionResult(
                    True,
                    f"🗳 {changed}: <b>{target_name}</b>.\nМожно изменить голос до конца этапа.",
                )

    # ------------------------------------------------------------- выход

    async def leave_game(self, game_id: int, user_id: int) -> ActionResult:
        """Выход во время игры = гибель игрока (cause=left) + проверка победы."""
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                games = GameRepository(session)
                game = await games.get(game_id)
                if game is None:
                    return ActionResult(False, "Игра не найдена.")
                if game.status not in ACTIVE_PHASES:
                    return ActionResult(False, "Игра уже завершена.")

                players_repo = GamePlayerRepository(session)
                gp = await players_repo.get_by_user(game.id, user_id)
                if gp is None:
                    return ActionResult(False, "Ты не участник этой игры.")
                if not gp.is_alive:
                    return ActionResult(False, "Ты уже выбыл из игры.")

                gp.is_alive = False
                gp.status = PlayerStatus.LEFT.value
                gp.died_at = utcnow()
                gp.death_cause = "left"
                self.phases._append_event(game, {
                    "type": "death", "day": game.day_number, "user_id": user_id,
                    "cause": "left", "killers": [],
                })
                await session.commit()

                players = await players_repo.list_for_game(game.id)
                name = esc(display_name(gp.user))
                await self.phases._broadcast(
                    players, f"🚪 <b>{name}</b> покинул игру и выбывает из города."
                )

                win = evaluate_win(players)
                if win is not None:
                    await self.phases._end_game(session, game, players, win)
                    await session.commit()
                else:
                    await self.notifier.send(
                        gp.user.telegram_id, game_view.death_personal_text(gp, "left")
                    )
                logger.info("Игра %s: игрок %s покинул игру", game.id, user_id)
                return ActionResult(True, "Ты покинул игру. Спасибо за партию!")

    # -------------------------------------------------------- просмотр

    async def get_status(self, game_id: int, user_id: int) -> ActionResult:
        async with self.session_factory() as session:
            game = await GameRepository(session).get(game_id)
            if game is None:
                return ActionResult(False, "Игра не найдена.")
            gp = await GamePlayerRepository(session).get_by_user(game.id, user_id)
            if gp is None:
                return ActionResult(False, "Ты не участник этой игры.")
            players = await GamePlayerRepository(session).list_for_game(game.id)
            text = game_view.game_status_text(game, gp, players)
            return ActionResult(True, text)
