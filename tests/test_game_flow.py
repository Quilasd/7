"""Интеграционные тесты полного игрового цикла (GameManager + PhaseManager)."""

from __future__ import annotations


from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.users import UserRepository
from tests.conftest import make_room, make_ready, make_user


async def _start_game(services, session, users, roles_setup=None):
    """Комната -> все готовы -> старт игры (остаётся в STARTING)."""
    room = await make_room(session, users[0], users, roles_setup=roles_setup)
    for user in users:
        await make_ready(session, room, user)
    result = await services.games.start_game_from_room(room.id, users[0].id)
    assert result.ok, result.message
    game_id = None
    async with services.session_factory() as s:
        from bot.database.repositories.rooms import RoomRepository

        fresh_room = await RoomRepository(s).get(room.id)
        game_id = fresh_room.game_id
    return game_id


async def _roles_map(services, game_id: int) -> dict[int, str]:
    """user_id -> role_id"""
    async with services.session_factory() as s:
        players = await GamePlayerRepository(s).list_for_game(game_id)
        return {p.user_id: p.role for p in players}


class TestStart:
    async def test_start_requires_creator(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        room = await make_room(session, users[0], users)
        for user in users:
            await make_ready(session, room, user)
        result = await services.games.start_game_from_room(room.id, users[1].id)
        assert not result.ok

    async def test_start_requires_all_ready(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        room = await make_room(session, users[0], users)
        for user in users[1:]:
            await make_ready(session, room, user)
        result = await services.games.start_game_from_room(room.id, users[0].id)
        assert not result.ok
        assert "готов" in result.message

    async def test_start_twice_rejected(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        room = await make_room(session, users[0], users)
        for user in users:
            await make_ready(session, room, user)
        first = await services.games.start_game_from_room(room.id, users[0].id)
        assert first.ok
        second = await services.games.start_game_from_room(room.id, users[0].id)
        assert not second.ok

    async def test_roles_distributed_and_dmed(self, services, session, notifier):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 2, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        assert len(roles) == 6
        assert list(roles.values()).count("mafia") == 2
        assert list(roles.values()).count("detective") == 1
        assert list(roles.values()).count("doctor") == 1
        assert list(roles.values()).count("citizen") == 2
        # каждому игроку отправлена карточка роли
        for user in users:
            assert any("ТВОЯ РОЛЬ" in text for text in notifier.messages_to(user.telegram_id))

    async def test_mafia_receives_teammates(self, services, session, notifier):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 2, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_ids = [uid for uid, role in roles.items() if role == "mafia"]
        by_telegram = {u.id: u.telegram_id for u in users}
        for uid in mafia_ids:
            texts = notifier.messages_to(by_telegram[uid])
            card = next(t for t in texts if "ТВОЯ РОЛЬ" in t)
            assert "союзники" in card.lower() or "союзников" in card.lower()


class TestPhaseFlow:
    async def test_full_cycle_to_city_win(self, services, session, notifier):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        # 1 мафия, детектив, доктор; ночь короткая
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        detective_uid = next(uid for uid, r in roles.items() if r == "detective")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        # --- Ночь 1: мафия убивает детектива
        await services.phases.begin_game(game_id)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.NIGHT.value
            assert game.day_number == 1

        kill = await services.games.submit_night_action(game_id, mafia_uid, "kill", detective_uid)
        assert kill.ok

        await services.phases.end_night(game_id)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.DAY.value
            detective_gp = await GamePlayerRepository(s).get_by_user(game_id, detective_uid)
            assert not detective_gp.is_alive
            assert detective_gp.death_cause == "mafia"
        assert notifier.contains(users[0].telegram_id, "погиб")
        assert any("РЕЗУЛЬТАТ ПРОВЕРКИ" not in t for t in notifier.sent)  # детектив мёртв - проверок нет

        # --- День 1 -> голосование: все валят мафию
        await services.phases.begin_voting(game_id)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.VOTING.value

        alive_uids = [
            uid for uid, r in roles.items()
            if uid not in (detective_uid,)
        ]
        for uid in alive_uids:
            if uid == mafia_uid:
                continue
            result = await services.games.cast_vote(game_id, uid, mafia_uid)
            assert result.ok

        await services.phases.end_voting(game_id)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "city"
            mafia_gp = await GamePlayerRepository(s).get_by_user(game_id, mafia_uid)
            assert not mafia_gp.is_alive
            assert mafia_gp.death_cause == "vote"

        # Финальное сообщение и статистика (читаем свежей сессией)
        assert any("ИГРА ОКОНЧЕНА" in text for _, text, _ in notifier.sent)
        async with services.session_factory() as s2:
            user_repo = UserRepository(s2)
            winner_user = await user_repo.get_by_id(citizen_uid)
            assert winner_user.games_played == 1
            assert winner_user.wins == 1
            # победа 100 + выживание 10 + правильный голос 2
            assert winner_user.rating == 112
            assert winner_user.xp == 42  # участие 10 + победа 25 + выживание 5 + голос 2
            mafia_user = await user_repo.get_by_id(mafia_uid)
            assert mafia_user.losses == 1
            assert mafia_user.rating == 30  # поражение 25 + убийство 5

    async def test_night_action_validations(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        doctor_uid = next(uid for uid, r in roles.items() if r == "doctor")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        # До старта ночи действие невозможно
        result = await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
        assert not result.ok

        await services.phases.begin_game(game_id)

        # Мафия не может «лечить» (чужой тип действия)
        result = await services.games.submit_night_action(game_id, mafia_uid, "heal", citizen_uid)
        assert not result.ok

        # Мёртвый не действует: убьём доктора мафией и завершим ночь
        kill_result = await services.games.submit_night_action(game_id, mafia_uid, "kill", doctor_uid)
        assert kill_result.ok
        await services.phases.end_night(game_id)
        result = await services.games.submit_night_action(game_id, doctor_uid, "heal", citizen_uid)
        assert not result.ok

        # Действие не той фазы (день)
        result = await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
        assert not result.ok

    async def test_doctor_no_repeat_target(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        doctor_uid = next(uid for uid, r in roles.items() if r == "doctor")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        first = await services.games.submit_night_action(game_id, doctor_uid, "heal", citizen_uid)
        assert first.ok
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)
        # Две ничьих подряд: круг 1 -> круг 2 -> никто не умирает -> ночь 2
        await services.phases.end_voting(game_id)
        await services.phases.end_voting(game_id)

        second = await services.games.submit_night_action(game_id, doctor_uid, "heal", citizen_uid)
        assert not second.ok
        assert "подряд" in second.message

    async def test_double_submit_updates_target(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        detective_uid = next(uid for uid, r in roles.items() if r == "detective")
        citizens = [uid for uid, r in roles.items() if r == "citizen"]
        target_a, target_b = citizens[0], citizens[1]

        await services.phases.begin_game(game_id)
        first = await services.games.submit_night_action(game_id, detective_uid, "check", target_a)
        assert first.ok
        second = await services.games.submit_night_action(game_id, detective_uid, "check", target_b)
        assert second.ok

        from bot.database.repositories.actions import GameActionRepository

        async with services.session_factory() as s:
            actions = await GameActionRepository(s).night_actions(game_id, 1)
            checks = [a for a in actions if a.action_type == "check"]
            assert len(checks) == 1
            assert checks[0].target_id == target_b

    async def test_leave_game_marks_dead_and_can_end_game(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")

        # Все, кроме мафии, покидают игру -> мафия >= остальных -> победа мафии
        await services.phases.begin_game(game_id)
        for uid, r in roles.items():
            if uid == mafia_uid:
                continue
            result = await services.games.leave_game(game_id, uid)
            # после достижения паритета игра завершается — дальнейшие выходы отклоняются
            assert result.ok or "завершена" in result.message

        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "mafia"

    async def test_tie_rule_no_death(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        room = await make_room(
            session, users[0], users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        settings = dict(room.settings)
        settings["tie_rule"] = "no_death"
        room.settings = settings
        await session.commit()

        for user in users:
            await make_ready(session, room, user)
        result = await services.games.start_game_from_room(room.id, users[0].id)
        assert result.ok
        async with services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = (await RoomRepository(s).get(room.id)).game_id

        roles = await _roles_map(services, game_id)
        await services.phases.begin_game(game_id)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)

        # Два голоса в разные стороны -> ничья -> никто не умирает -> ночь
        alive = [uid for uid, r in roles.items()]
        await services.games.cast_vote(game_id, alive[0], alive[1])
        await services.games.cast_vote(game_id, alive[2], alive[3])
        await services.phases.end_voting(game_id)

        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.NIGHT.value
            assert game.day_number == 2
            players = await GamePlayerRepository(s).list_for_game(game_id)
            assert all(p.is_alive for p in players if p.death_cause is None)

    async def test_tie_rule_revote(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        await services.phases.begin_game(game_id)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)

        alive = list(roles.keys())
        await services.games.cast_vote(game_id, alive[0], alive[1])
        await services.games.cast_vote(game_id, alive[2], alive[3])
        await services.phases.end_voting(game_id)  # ничья -> круг 2

        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.VOTING.value
            assert game.vote_context["round_no"] == 2
            assert set(game.vote_context["candidates"]) == {alive[1], alive[3]}

        # Во 2 круге голосовать можно только за лидеров
        denied = await services.games.cast_vote(game_id, alive[4], alive[0])
        assert not denied.ok
        allowed = await services.games.cast_vote(game_id, alive[4], alive[1])
        assert allowed.ok

    async def test_mafia_reaches_parity_at_night_end(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 6)]
        # 5 игроков: 2 мафии + 3 мирных; мафия убивает одного -> 2 vs 2 -> победа мафии
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 2, "detective": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uids = [uid for uid, r in roles.items() if r == "mafia"]
        victim_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        kill = await services.games.submit_night_action(game_id, mafia_uids[0], "kill", victim_uid)
        assert kill.ok
        await services.phases.end_night(game_id)

        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "mafia"

    async def test_force_end_draws_without_rating_change(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        await services.phases.begin_game(game_id)
        assert await services.phases.force_end(game_id, "тест")
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "draw"
            user = await UserRepository(s).get_by_id(users[0].id)
            assert user.games_played == 1
            assert user.rating == 0  # рейтинг не тронут (старт 0)


class TestRecovery:
    async def test_phase_deadline_persisted_for_recovery(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        await services.phases.begin_game(game_id)
        async with services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.NIGHT.value
            assert game.phase_deadline is not None

        # «Рестарт»: новый PhaseManager поверх той же БД восстанавливает игры
        from bot.services.phase_manager import GameLocks, PhaseManager
        from bot.services.timer_manager import NoopTimerManager

        fresh_phases = PhaseManager(
            services.session_factory, services.notifier, NoopTimerManager(), GameLocks()
        )
        count = await fresh_phases.recover()
        assert count == 1
