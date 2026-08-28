"""Менеджер фаз: машина состояний игры WAITING→STARTING→NIGHT→DAY→VOTING→ENDED.

Каждый переход:
1. берёт лок игры (сериализация с действиями пользователей);
2. перечитывает состояние из БД и проверяет, что фаза соответствует ожидаемой
   (защита от «двойных» таймеров и устаревших вызовов);
3. применяет переход и фиксирует его в БД;
4. рассылает уведомления и планирует следующий таймер.

Методы-обёртки (begin_game/end_night/…) предназначены для таймеров и
recover() после рестарта, внутренние (_…) выполняются под локом.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.database.models import Game, GamePlayer, GameStatus, PlayerStatus, RoomStatus, WinningSide
from bot.database.repositories.actions import GameActionRepository
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.social import DeathNoteRepository
from bot.database.repositories.users import UserRepository
from bot.roles import ActionType, Team, get_role, team_of
from bot.services import game_view
from bot.services.night_resolver import resolve_night
from bot.services.notifier import Notifier
from bot.services.rating import AppliedStats, RatingService, ScopeFlags, StatEvents
from bot.services.role_manager import WinResult, evaluate_win
from bot.services.timer_manager import TimerManager
from bot.services.vote_manager import VoteManager
from bot.utils.helpers import deadline_in, display_name, esc, future, utcnow

logger = logging.getLogger(__name__)

ACTIVE_PHASES = (GameStatus.STARTING.value, GameStatus.NIGHT.value, GameStatus.DAY.value, GameStatus.VOTING.value)


class GameLocks:
    """Реестр локов по game_id — общий для фаз и действий игроков."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, game_id: int) -> asyncio.Lock:
        if game_id not in self._locks:
            self._locks[game_id] = asyncio.Lock()
        return self._locks[game_id]


class PhaseManager:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        notifier: Notifier,
        timers: TimerManager,
        locks: GameLocks | None = None,
        rating: RatingService | None = None,
        app_settings=None,
        game_chats=None,
    ) -> None:
        self.session_factory = session_factory
        self.notifier = notifier
        self.timers = timers
        self.locks = locks or GameLocks()
        self.rating = rating or RatingService()
        # app_settings: объект с флагами DEBUG_AFFECTS_* (bot.config.Settings)
        self.app_settings = app_settings
        # GameChatService | None — игровые чаты партии (необязательный слой)
        self.game_chats = game_chats

    async def _chats_call(self, method_name: str, *args) -> None:
        """Хук игровых чатов: сбой чатов НЕ ломает игровой поток."""
        service = getattr(self, "game_chats", None)
        if service is None:
            return
        try:
            await getattr(service, method_name)(*args)
        except Exception:
            logger.warning(
                "GameChat: сбой синхронизации чатов (игра продолжается)",
                exc_info=True,
            )

    # ------------------------------------------------------------------ utils

    async def _send(self, telegram_id: int, text: str, keyboard=None) -> None:
        await self.notifier.send(telegram_id, text, keyboard)

    def _schedule(self, game_id: int, phase: str, seconds: int, callback) -> None:
        self.timers.cancel(game_id)
        self.timers.schedule(game_id, phase, seconds, callback)

    async def _broadcast(self, players: list[GamePlayer], text: str, keyboard=None) -> None:
        for gp in players:
            if gp.status != PlayerStatus.SPECTATOR.value:
                await self._send(gp.user.telegram_id, text, keyboard)

    @staticmethod
    def _append_event(game: Game, event: dict) -> None:
        # JSON-колонки не отслеживают in-place мутации — переприсваиваем
        game.events = list(game.events or []) + [event]

    # ------------------------------------------------------- предсмертные записки

    async def _record_death_note(self, session, game: Game, victim: GamePlayer) -> None:
        """Создаёт placeholder записки умершего и предлагает её написать."""
        from bot.keyboards.game import death_note_keyboard

        await DeathNoteRepository(session).ensure(game.id, victim.user_id, game.day_number)
        await self._send(
            victim.user.telegram_id,
            "📝 Ты можешь оставить <b>предсмертную записку</b> (до 300 символов) — "
            "её прочитают утром. Один раз, изменить нельзя.\n\n"
            "Напиши: <code>/note &lt;текст&gt;</code> или нажми кнопку.",
            death_note_keyboard(game.id),
        )

    async def _publish_morning_notes(self, session, game: Game, players: list[GamePlayer]) -> None:
        """Публикует записки умерших ранее (death_day < текущего дня) утром."""
        repo = DeathNoteRepository(session)
        pending = await repo.pending_before(game.id, game.day_number)
        if not pending:
            return
        by_id = {p.user_id: p for p in players}
        for note in pending:
            victim = by_id.get(note.user_id)
            await self._broadcast(players, game_view.death_note_text(victim, note.text))
            note.published = True
            note.published_at = utcnow()
        await session.flush()

    # ------------------------------------------------------- публичные фазы

    async def valid_night_targets(self, session, game: Game, actor: GamePlayer, players: list[GamePlayer]) -> list[GamePlayer]:
        """Публичный доступ к расчёту допустимых целей (для UI-хендлеров)."""
        return await self._valid_night_targets(session, game, actor, players)

    async def begin_game(self, game_id: int) -> None:
        """Завершение отсчёта STARTING -> NIGHT."""
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                game = await GameRepository(session).get(game_id)
                if not game or game.status != GameStatus.STARTING.value:
                    return
                await self._begin_night(session, game)
                await session.commit()

    async def end_night(self, game_id: int) -> None:
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                game = await GameRepository(session).get(game_id)
                if not game or game.status != GameStatus.NIGHT.value:
                    return
                players_repo = GamePlayerRepository(session)
                players = await players_repo.list_for_game(game.id)
                actions = await GameActionRepository(session).night_actions(game.id, game.day_number)
                settings = game.settings or {}
                outcome = resolve_night(actions, players)

                by_user_id = {p.user_id: p for p in players}
                for death in outcome.deaths:
                    victim = by_user_id.get(death.user_id)
                    if victim and victim.is_alive:
                        victim.is_alive = False
                        victim.status = PlayerStatus.DEAD.value
                        victim.died_at = utcnow()
                        victim.death_cause = death.cause
                        self._append_event(game, {
                            "type": "death",
                            "day": game.day_number,
                            "user_id": death.user_id,
                            "cause": death.cause,
                            "killers": death.killers,
                        })
                for save in outcome.saves:
                    self._append_event(game, {
                        "type": "save",
                        "day": game.day_number,
                        "user_id": save.healer_id,
                        "target_id": save.target_id,
                    })
                await session.commit()

                for death in outcome.deaths:
                    victim = by_user_id.get(death.user_id)
                    if victim:
                        await self._chats_call("on_death", session, game, victim)

                win = evaluate_win(players)
                if win is not None:
                    await self._end_game(session, game, players, win)
                    await session.commit()
                    return

                # Утренняя сводка + личные сообщения погибшим и комиссару
                morning = game_view.morning_text(
                    game, outcome, players, settings.get("reveal_roles_on_death", True)
                )
                await self._broadcast(players, morning)
                # публикуем записки умерших ранее (умерли до этого дня)
                await self._publish_morning_notes(session, game, players)
                for death in outcome.deaths:
                    victim = by_user_id.get(death.user_id)
                    if victim:
                        await self._send(
                            victim.user.telegram_id,
                            game_view.death_personal_text(victim, death.cause),
                        )
                        # предложить предсмертную записку (опубликуется следующим утром)
                        await self._record_death_note(session, game, victim)
                for check in outcome.checks:
                    detective = by_user_id.get(check.detective_id)
                    target = by_user_id.get(check.target_id)
                    if detective and target:
                        verdict = "🔴 мафия!" if check.target_is_mafia else "🔵 не мафия."
                        await self._send(
                            detective.user.telegram_id,
                            f"🕵️ <b>РЕЗУЛЬТАТ ПРОВЕРКИ</b>\n\n"
                            f"{esc(display_name(target.user))} — {verdict}",
                        )

                await self._begin_day(session, game, players)
                await session.commit()

    async def begin_voting(self, game_id: int) -> None:
        """DAY -> VOTING (вызывается таймером дня)."""
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                game = await GameRepository(session).get(game_id)
                if not game or game.status != GameStatus.DAY.value:
                    return
                players = await GamePlayerRepository(session).list_for_game(game.id)
                await self._begin_voting(session, game, players, round_no=1, candidates=None)
                await session.commit()

    async def end_voting(self, game_id: int) -> None:
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                game = await GameRepository(session).get(game_id)
                if not game or game.status != GameStatus.VOTING.value:
                    return
                await self._end_voting(session, game)
                await session.commit()

    # ------------------------------------------------------- внутренние фазы

    async def _begin_night(self, session, game: Game) -> None:
        from bot.keyboards.game import night_action_keyboard

        players_repo = GamePlayerRepository(session)
        players = await players_repo.list_for_game(game.id)
        settings = game.settings or {}

        game.status = GameStatus.NIGHT.value
        game.day_number += 1
        seconds = int(settings.get("night_seconds", 90))
        game.phase_deadline = future(seconds)
        await session.commit()

        await self._broadcast(players, game_view.night_header(game))
        for gp in players:
            if not gp.is_alive:
                continue
            role = get_role(gp.role)
            if role and role.night_action:
                targets = await self._valid_night_targets(session, game, gp, players)
                keyboard = night_action_keyboard(game.id, role, targets, gp)
                await self._send(
                    gp.user.telegram_id,
                    f"{game_view.night_header(game)}\n\n{role.emoji} <b>{role.action_prompt}</b>\n"
                    f"⏱ Время: {seconds // 60:02d}:{seconds % 60:02d}",
                    keyboard,
                )
        self._schedule(game.id, "night", seconds, partial(self.end_night, game.id))
        await self._chats_call("on_night_started", session, game, players)
        logger.info("Игра %s: ночь #%s начата", game.id, game.day_number)

    async def _valid_night_targets(
        self, session, game: Game, actor: GamePlayer, players: list[GamePlayer]
    ) -> list[GamePlayer]:
        """Допустимые цели с учётом правил роли."""
        role = get_role(actor.role)
        if not role:
            return []
        targets = []
        for p in players:
            if not p.is_alive:
                continue
            if p.user_id == actor.user_id and not role.can_target_self:
                continue
            if role.night_action == ActionType.KILL and team_of(p.role) == role.team:
                continue  # мафия не стреляет по своим
            targets.append(p)

        if role.no_repeat_target and game.day_number > 1:
            actions_repo = GameActionRepository(session)
            prev = await actions_repo.get_action(
                game.id, actor.user_id, role.night_action.value, game.day_number - 1
            )
            if prev:
                targets = [p for p in targets if p.user_id != prev.target_id]
        return targets

    async def _begin_day(self, session, game: Game, players: list[GamePlayer]) -> None:
        settings = game.settings or {}
        seconds = int(settings.get("day_seconds", 180))
        game.status = GameStatus.DAY.value
        game.phase_deadline = future(seconds)
        await session.commit()

        alive = [p for p in players if p.is_alive]
        text = game_view.day_text(game, players, seconds)
        for gp in alive:
            await self._send(gp.user.telegram_id, text)
        self._schedule(game.id, "day", seconds, partial(self.begin_voting, game.id))
        await self._chats_call("on_day_started", session, game, players)
        logger.info("Игра %s: день #%s начат", game.id, game.day_number)

    async def _begin_voting(
        self,
        session,
        game: Game,
        players: list[GamePlayer],
        round_no: int,
        candidates: list[int] | None,
    ) -> None:
        from bot.keyboards.game import vote_keyboard

        settings = game.settings or {}
        seconds = int(settings.get("vote_seconds", 60))
        game.status = GameStatus.VOTING.value
        game.phase_deadline = future(seconds)
        game.vote_context = {"round_no": round_no, "candidates": candidates}

        alive = [p for p in players if p.is_alive]
        if candidates is None:
            candidate_ids = [p.user_id for p in alive]
        else:
            candidate_ids = list(candidates)
        cand_players = [p for p in alive if p.user_id in candidate_ids]
        await session.commit()

        for gp in alive:
            keyboard = vote_keyboard(game.id, round_no, cand_players, gp)
            await self._send(
                gp.user.telegram_id,
                game_view.voting_text(game, cand_players, seconds, round_no),
                keyboard,
            )
        self._schedule(game.id, "voting", seconds, partial(self.end_voting, game.id))
        logger.info("Игра %s: голосование (день %s, круг %s)", game.id, game.day_number, round_no)

    async def _end_voting(self, session, game: Game) -> None:
        settings = game.settings or {}
        players_repo = GamePlayerRepository(session)
        players = await players_repo.list_for_game(game.id)
        vote_manager = VoteManager(session)
        resolution = await vote_manager.resolve(game)
        round_no = vote_manager.current_round(game)

        await self._broadcast(players, game_view.vote_results_text(game, resolution, players))

        if resolution.is_tie:
            tie_rule = settings.get("tie_rule", "revote")
            if tie_rule == "revote" and round_no == 1:
                await self._broadcast(
                    players,
                    "⚖️ Ничья! Запускаю повторное голосование среди лидеров.\n"
                    "Если и сейчас ничья — никто не покинет город.",
                )
                await self._begin_voting(session, game, players, round_no + 1, resolution.tied_ids)
                return
            await self._broadcast(players, "🌿 День завершается без казни.")

        else:
            victim = next((p for p in players if p.user_id == resolution.lynched), None)
            if victim:
                victim.is_alive = False
                victim.status = PlayerStatus.DEAD.value
                victim.died_at = utcnow()
                victim.death_cause = "vote"
                self._append_event(game, {
                    "type": "lynch",
                    "day": game.day_number,
                    "round": round_no,
                    "user_id": victim.user_id,
                    "voters": resolution.voters_by_target.get(victim.user_id, []),
                })
                await self._send(
                    victim.user.telegram_id,
                    game_view.death_personal_text(victim, "vote"),
                )
                # предложить предсмертную записку — опубликуется следующим утром,
                # текущую фазу/переход к ночи не ломаем
                await self._record_death_note(session, game, victim)
                await session.commit()
                await self._chats_call("on_death", session, game, victim)

        win = evaluate_win(players)
        if win is not None:
            await self._end_game(session, game, players, win)
            await session.commit()
            return

        game.vote_context = None
        await self._begin_night(session, game)
        await session.commit()

    # ------------------------------------------------------------ конец игры

    async def _end_game(
        self,
        session,
        game: Game,
        players: list[GamePlayer],
        win: WinResult | None,
        reason: str | None = None,
    ) -> None:
        if game.status == GameStatus.ENDED.value:
            # защита от двойного финала: повторный вызов не должен повторно
            # начислять статистику/XP/достижения (идемпотентность)
            logger.warning(
                "Игра %s: _end_game вызван повторно (уже ENDED) — финализация пропущена",
                game.id,
            )
            return
        logger.info("GAME FINISH START: игра %s", game.id)
        game.status = GameStatus.ENDED.value
        game.ended_at = utcnow()
        game.phase_deadline = None
        game.vote_context = None
        if win is not None:
            game.winner = win.side.value
            title = win.title
            winner_ids = set(win.winner_user_ids)
        else:
            game.winner = WinningSide.DRAW.value
            game.end_reason = reason or "Игра принудительно завершена"
            title = "🛑 ИГРА ОСТАНОВЛЕНА"
            winner_ids = set()

        if game.room_id:
            room = await RoomRepository(session).get(game.room_id)
            if room and room.status != RoomStatus.CLOSED.value:
                room.status = RoomStatus.FINISHED.value
                room.closed_at = utcnow()

        await session.commit()
        self.timers.cancel(game.id)

        # --- Статистика и рейтинг (глобально и локально — раздельно) --------
        test_mode = bool(game.get_setting("test_mode"))
        side = WinningSide(game.winner)
        is_draw = side == WinningSide.DRAW

        # Настройки группы (если игра привязана к группе) задают флаги скоупов
        group_settings = None
        if game.group_id:
            from bot.database.repositories.groups import GroupSettingsRepository

            group_settings = await GroupSettingsRepository(session).get_for(game.group_id)

        def flag(name: str) -> bool:
            return bool(getattr(group_settings, name, True)) if group_settings else True

        # Личный вклад за игру (для обоих скоупов один и тот же)
        events = StatEvents()
        for event in game.events or []:
            if event.get("type") == "death" and event.get("cause") in ("mafia", "maniac"):
                for killer_id in event.get("killers", []):
                    events.kills[killer_id] = events.kills.get(killer_id, 0) + 1
            elif event.get("type") == "save":
                healer = event.get("user_id")
                events.saves[healer] = events.saves.get(healer, 0) + 1
            elif event.get("type") == "lynch":
                victim = next((p for p in players if p.user_id == event.get("user_id")), None)
                if victim is not None and team_of(victim.role) == Team.MAFIA:
                    for voter_id in event.get("voters", []):
                        voter = next((p for p in players if p.user_id == voter_id), None)
                        if voter is not None and team_of(voter.role) == Team.CITY:
                            events.correct_votes[voter_id] = events.correct_votes.get(voter_id, 0) + 1
        # Расследования: поданные проверки комиссара
        check_actions = await GameActionRepository(session).actions_of_type(game.id, "check")
        for action in check_actions:
            events.investigations[action.actor_id] = events.investigations.get(action.actor_id, 0) + 1
        survived_ids = {p.user_id for p in players if p.is_alive}

        global_flags = ScopeFlags(
            rating=flag("rating_enabled") and flag("global_rating_enabled"),
            xp=flag("xp_enabled") and flag("global_xp_enabled"),
        )
        local_flags = ScopeFlags(
            rating=flag("rating_enabled") and flag("local_rating_enabled"),
            xp=flag("xp_enabled") and flag("local_xp_enabled"),
        )
        debug_global = bool(self.app_settings.debug_affects_global_stats) if self.app_settings else False
        debug_local = bool(self.app_settings.debug_affects_local_stats) if self.app_settings else False
        apply_global = (not test_mode) or debug_global
        apply_local = bool(game.group_id) and ((not test_mode) or debug_local)

        applied_global = AppliedStats()
        applied_local = AppliedStats()
        wins_before: dict[int, int] = {}
        if apply_global:
            users_repo = UserRepository(session)
            users_by_id: dict[int, object] = {}
            for gp in players:
                user = await users_repo.get_by_id(gp.user_id)
                if user:
                    users_by_id[gp.user_id] = user
            wins_before = {uid: int(u.wins) for uid, u in users_by_id.items() if u is not None}
            applied_global = self.rating.apply_global(
                users_by_id, winner_ids, is_draw, events, survived_ids, global_flags
            )
        if apply_local:
            from bot.database.repositories.groups import GroupPlayerRepository

            gp_repo = GroupPlayerRepository(session)
            local_rows: dict[int, object] = {}
            for gp in players:
                local_rows[gp.user_id] = await gp_repo.ensure(game.group_id, gp.user_id)
            applied_local = self.rating.apply_local(
                local_rows, winner_ids, is_draw, events, survived_ids, local_flags
            )
        if test_mode and not (apply_global or apply_local):
            logger.info("Игра %s: тестовый режим — статистика не обновляется", game.id)
        await session.commit()
        if apply_global or apply_local:
            logger.info(
                "STATS UPDATED: игра %s (global=%s, local=%s, победителей=%s)",
                game.id, apply_global, apply_local, len(winner_ids),
            )

        # --- Достижения, титулы и финал предсмертных записок -----------------
        by_id = {p.user_id: p for p in players}
        newly_awarded: dict[int, list] = {}
        if not is_draw:
            from bot.services import achievements as ach
            from bot.services import rewards as rw

            roles_map = {
                gp.user_id: gp.role for gp in players
                if gp.status != PlayerStatus.SPECTATOR.value
            }
            correct_checks: dict[int, int] = {}
            for action in check_actions:
                target_gp = by_id.get(action.target_id)
                if target_gp and team_of(target_gp.role) == Team.MAFIA:
                    correct_checks[action.actor_id] = correct_checks.get(action.actor_id, 0) + 1
            sacrificed_ids = {
                e.get("user_id") for e in (game.events or [])
                if e.get("type") == "death" and e.get("cause") == "sacrifice"
            }
            maniac_killers: set[int] = set()
            for e in (game.events or []):
                if e.get("type") == "death" and e.get("cause") == "maniac":
                    maniac_killers.update(e.get("killers", []))
            win_streak_after = {}
            if apply_global:
                win_streak_after = {
                    uid: int(getattr(u, "win_streak", 0) or 0) for uid, u in users_by_id.items()
                }
            ctx = ach.GameAchievementContext(
                roles=roles_map, winners=winner_ids, is_draw=is_draw,
                survived_ids=survived_ids, kills=events.kills, saves=events.saves,
                correct_checks=correct_checks, correct_votes=events.correct_votes,
                sacrificed_ids=sacrificed_ids, maniac_killers=maniac_killers,
                wins_before=wins_before, win_streak_after=win_streak_after,
            )
            newly_awarded = await rw.award_achievements(session, ach.evaluate(ctx))
            logger.info(
                "ACHIEVEMENTS EVALUATED: игра %s, новых наград: %s",
                game.id, sum(len(v) for v in newly_awarded.values()),
            )

        # опубликовать оставшиеся неопубликованные записки (чтобы не потерять)
        for note in await DeathNoteRepository(session).unpublished(game.id):
            victim = by_id.get(note.user_id)
            await self._broadcast(players, game_view.death_note_text(victim, note.text))
            note.published = True
            note.published_at = utcnow()
        await session.commit()

        # --- Финальные сообщения (одно на игрока, без лишнего спама) --------
        overview = game_view.game_over_text(game, title, players, winner_ids, reason or game.end_reason)
        for gp in players:
            if gp.status == PlayerStatus.SPECTATOR.value:
                continue
            if test_mode and not (apply_global or apply_local):
                personal = "🧪 Тестовая игра — статистика и рейтинг не изменены."
            elif is_draw:
                personal = "🤝 Ничья — рейтинг и опыт не изменены."
            else:
                parts = []
                if apply_global:
                    parts.append(
                        f"🌐 Рейтинг: {applied_global.rating_delta.get(gp.user_id, 0):+d}, "
                        f"опыт: {applied_global.xp_delta.get(gp.user_id, 0):+d}"
                    )
                if apply_local:
                    parts.append(
                        f"🏠 Группа: рейтинг {applied_local.rating_delta.get(gp.user_id, 0):+d}, "
                        f"опыт {applied_local.xp_delta.get(gp.user_id, 0):+d}"
                    )
                won = gp.user_id in winner_ids
                head = "🎉 Ты в числе победителей!" if won else "😔 Поражение."
                personal = head + "\n" + "\n".join(parts)
                # 🎉 повышение уровня (одно сообщение, даже если уровней несколько)
                from bot.utils.helpers import xp_progress_lines

                lu_global = (applied_global.level_ups.get(gp.user_id)
                             if apply_global else None)
                lu_local = (applied_local.level_ups.get(gp.user_id)
                            if apply_local else None)
                if lu_global or lu_local:
                    old_level, new_level = lu_global or lu_local
                    progress = xp_progress_lines(
                        users_by_id[gp.user_id].xp if apply_global
                        else gp.user.xp
                    )
                    personal += (
                        f"\n\n🎉 <b>НОВЫЙ УРОВЕНЬ: {old_level} → {new_level}!</b>\n"
                        f"📈 Уровень: <b>{new_level}</b>\n" + "\n".join(progress)
                    )
                    if lu_local and (not lu_global or lu_local[1] != lu_global[1]):
                        personal += f"\n🏠 В группе: уровень {lu_local[1]}"
            awards = newly_awarded.get(gp.user_id)
            if awards:
                from bot.services import titles as _t
                lines = [f"🏅 Новое достижение: {a.name}." for a in awards]
                unlocked_titles = []
                for a in awards:
                    tkey = _t.TITLE_UNLOCKS.get(a.id)
                    if tkey:
                        title = _t.get_title(tkey)
                        if title:
                            unlocked_titles.append(title.name)
                if unlocked_titles:
                    lines.append(f"🎓 Открыт титул: {', '.join(dict.fromkeys(unlocked_titles))}.")
                personal = personal + "\n" + "\n".join(lines)
            await self._send(gp.user.telegram_id, f"{overview}\n\n———\n{personal}")
        await self._chats_call("on_game_ended", session, game, players, title)
        logger.info(
            "GAME FINISH COMPLETE: игра %s, победитель=%s%s (global=%s, local=%s)",
            game.id, game.winner, " (тест)" if test_mode else "", apply_global, apply_local,
        )

    async def force_end(self, game_id: int, reason: str) -> bool:
        """Принудительное завершение (админ)."""
        async with self.locks.get(game_id):
            async with self.session_factory() as session:
                game = await GameRepository(session).get(game_id)
                if not game or game.status not in ACTIVE_PHASES:
                    return False
                players = await GamePlayerRepository(session).list_for_game(game.id)
                await self._end_game(session, game, players, None, reason)
                await session.commit()
                return True

    # ------------------------------------------------------------- restore

    async def recover(self) -> int:
        """Восстановление активных игр после рестарта бота."""
        async with self.session_factory() as session:
            games = await GameRepository(session).active_games()
        recovered = 0
        callbacks = {
            GameStatus.STARTING.value: self.begin_game,
            GameStatus.NIGHT.value: self.end_night,
            GameStatus.DAY.value: self.begin_voting,
            GameStatus.VOTING.value: self.end_voting,
        }
        for game in games:
            callback = callbacks.get(game.status)
            if callback is None:
                continue
            delay = deadline_in(game.phase_deadline)
            if delay <= 0:
                delay = 1  # дедлайн прошёл — отрабатываем почти сразу
            self.timers.schedule(game.id, f"recover-{game.status}", delay, partial(callback, game.id))
            recovered += 1
        if self.game_chats is not None:
            try:
                chats_recovered = await self.game_chats.recover(session)
                if chats_recovered:
                    logger.info("Восстановлено прав игровых чатов: %s", chats_recovered)
            except Exception:
                logger.warning("GameChat: сбой восстановления чатов", exc_info=True)
        if recovered:
            logger.info("Восстановлено активных игр: %s", recovered)
        return recovered
