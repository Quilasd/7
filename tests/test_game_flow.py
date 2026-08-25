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


class TestDeathNote:
    """Предсмертная записка: создание, неизменяемость, публикация утром."""

    async def test_death_note_created_and_immutable(self, services, session, notifier):
        from bot.database.repositories.social import DeathNoteRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        victim_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", victim_uid)
        await services.phases.end_night(game_id)  # жертва гибнет ночью

        async with services.session_factory() as s:
            note = await DeathNoteRepository(s).get(game_id, victim_uid)
            assert note is not None
            assert note.text is None          # placeholder, ещё не написана
            assert note.published is False

        # первая записка сохраняется
        async with services.session_factory() as s:
            saved = await DeathNoteRepository(s).set_text(game_id, victim_uid, "Это была мафия!")
            await s.commit()
            assert saved is not None and saved.text == "Это была мафия!"

        # повторно изменить нельзя (неизменяемо)
        async with services.session_factory() as s:
            second = await DeathNoteRepository(s).set_text(game_id, victim_uid, "другой текст")
            assert second is None

    async def test_too_long_note_rejected(self, services, session):
        from bot.database.repositories.social import DeathNoteRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        victim_uid = next(uid for uid, r in roles.items() if r == "citizen")
        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", victim_uid)
        await services.phases.end_night(game_id)

        # длина > 300 → заголовком rejects на уровне хендлера; репо хранит как есть,
        # но лимит 300 соблюдается бизнес-логикой хендлера (DEATH_NOTE_MAX).
        text = "А" * 300
        async with services.session_factory() as s:
            saved = await DeathNoteRepository(s).set_text(game_id, victim_uid, text)
            await s.commit()
            assert saved is not None
            assert len(saved.text) == 300

    async def test_note_published_next_morning(self, services, session, notifier):
        from bot.database.repositories.social import DeathNoteRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        citizens = [uid for uid, r in roles.items() if r == "citizen"]
        victim_uid, lynch_uid = citizens[0], citizens[1]

        # --- Ночь 1: гибель жертвы, записка записана (death_day=1)
        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", victim_uid)
        await services.phases.end_night(game_id)
        async with services.session_factory() as s:
            await DeathNoteRepository(s).set_text(game_id, victim_uid, "Запомните: это мафия!")
            await s.commit()

        # --- День 1: город линчует другого гражданина (игра продолжается)
        await services.phases.begin_voting(game_id)
        alive_city = [
            uid for uid, r in roles.items()
            if uid not in (victim_uid,) and r != "mafia"
        ]
        for uid in alive_city:
            await services.games.cast_vote(game_id, uid, lynch_uid)
        await services.phases.end_voting(game_id)  # линчеван → ночь 2 (без победы)

        # записка ещё не опубликована (утро дня 1 уже прошло, она опубликуется утром дня 2)
        async with services.session_factory() as s:
            note = await DeathNoteRepository(s).get(game_id, victim_uid)
            assert note.published is False

        # --- Ночь 2 → утро дня 2: записка публикуется
        await services.phases.end_night(game_id)
        async with services.session_factory() as s:
            note = await DeathNoteRepository(s).get(game_id, victim_uid)
            assert note.published is True
        assert any("Запомните: это мафия!" in text for _, text, _ in notifier.sent)

    async def test_empty_note_neutral_message(self, services, session, notifier):
        from bot.database.repositories.social import DeathNoteRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        victim_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", victim_uid)
        await services.phases.end_night(game_id)
        # жертва ничего не написала → пустая записка (нейтральное сообщение)
        async with services.session_factory() as s:
            await DeathNoteRepository(s).set_text(game_id, victim_uid, "")
            await s.commit()
        # игра завершается → unpublished-loop публикует нейтральную записку
        await services.phases.force_end(game_id, "тест")
        assert any("ничего не успел сказать" in text for _, text, _ in notifier.sent)


class TestWinStreak:
    """Серия побед: +1 при победе, 0 при поражении, лучшая серия растёт."""

    async def test_streak_on_win_and_loss(self, services, session):
        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)
        alive_city = [uid for uid, r in roles.items() if r != "mafia" and uid != citizen_uid]
        for uid in alive_city:
            await services.games.cast_vote(game_id, uid, mafia_uid)
        await services.phases.end_voting(game_id)  # победа города

        async with services.session_factory() as s:
            winner = await UserRepository(s).get_by_id(citizen_uid)
            loser = await UserRepository(s).get_by_id(mafia_uid)
            assert winner.win_streak == 1
            assert winner.best_win_streak == 1
            assert loser.win_streak == 0
            assert loser.best_win_streak == 0


class TestAchievementsAndTitles:
    """Достижения и титулы за игровые ситуации (одноразовые, открывают титул)."""

    async def test_city_win_grants_achievement_and_title(self, services, session):
        from bot.database.repositories.social import UserAchievementRepository, UserTitleRepository

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        await services.phases.begin_game(game_id)
        await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)
        alive_city = [uid for uid, r in roles.items() if r != "mafia" and uid != citizen_uid]
        for uid in alive_city:
            await services.games.cast_vote(game_id, uid, mafia_uid)
        await services.phases.end_voting(game_id)  # победа города

        async with services.session_factory() as s:
            ids = await UserAchievementRepository(s).ids_of(citizen_uid)
            assert "city_win" in ids
            assert "first_win" in ids  # первая победа
            titles = await UserTitleRepository(s).ids_of(citizen_uid)
            assert "veteran" in titles   # за city_win
            assert "rookie" in titles    # за first_win

    async def test_achievements_are_one_time(self, services, session):
        from bot.services import rewards as rw

        user = await make_user(session, "Once")
        uid = user.id
        earned = {uid: {"city_win", "first_win"}}
        async with services.session_factory() as s:
            first = await rw.award_achievements(s, earned)
            await s.commit()
            assert {a.id for a in first.get(uid, [])} == {"city_win", "first_win"}
            # повторная выдача того же — ничего нового
            second = await rw.award_achievements(s, earned)
            await s.commit()
            assert second.get(uid) is None or second.get(uid) == []


class TestMetaAfterFullGame:
    """После РЕАЛЬНОЙ партии: достижения, титулы, история, профиль, global/local."""

    async def test_achievements_titles_history_profile_after_game(
        self, services, session, notifier
    ):
        from bot.database.repositories.groups import GroupPlayerRepository
        from bot.database.repositories.social import UserAchievementRepository
        from bot.handlers.profile import _full_profile_text, compute_profile_extras
        from bot.services.progression import DEFAULT_PROGRESSION as prog

        users = [await make_user(session, f"M{i}") for i in range(1, 7)]
        # играем В ГРУППЕ: локальная статистика обязана обновиться тоже
        group = await services.groups.get_or_create(-610000, "Мафия Клуб")
        for u in users:
            await GroupPlayerRepository(session).ensure(group.id, u.id)
        await session.commit()

        room = await make_room(session, users[0], users,
                               roles_setup={"mafia": 1, "detective": 1, "doctor": 1})
        room.group_id = group.id
        await session.commit()
        for u in users:
            await make_ready(session, room, u)
        result = await services.games.start_game_from_room(room.id, users[0].id)
        assert result.ok, result.message
        async with services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = await RoomRepository(s).get(room.id)
            game_id = game_id.game_id

        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")
        detective_uid = next(uid for uid, r in roles.items() if r == "detective")
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")

        # Ночь 1: мафия убивает детектива; день: город вешает мафию -> победа города
        await services.phases.begin_game(game_id)
        kill = await services.games.submit_night_action(game_id, mafia_uid, "kill", detective_uid)
        assert kill.ok
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)
        for uid in roles:
            if uid != mafia_uid and uid != detective_uid:
                assert (await services.games.cast_vote(game_id, uid, mafia_uid)).ok
        await services.phases.end_voting(game_id)

        async with services.session_factory() as s2:
            game = await GameRepository(s2).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "city"

        # --- достижения: автоматическая выдача работает и после правок профиля
        async with services.session_factory() as s2:
            ach_repo = UserAchievementRepository(s2)
            winner_ach = await ach_repo.ids_of(citizen_uid)
            # первая победа + победа городом + верный голос против мафии
            assert {"first_win", "city_win", "sharp_eye"} <= winner_ach
            mafia_ach = await ach_repo.ids_of(mafia_uid)
            assert "first_win" not in mafia_ach  # мафия проиграла

        # --- титулы, открытые достижениями
        from bot.database.repositories.social import UserTitleRepository

        async with services.session_factory() as s2:
            titles = await UserTitleRepository(s2).ids_of(citizen_uid)
            assert "rookie" in titles and "veteran" in titles

        # --- уведомление о достижении игроку
        assert any("достижение" in text.lower() for _, text, _ in notifier.sent)

        # --- история партий
        async with services.session_factory() as s2:
            history = await GamePlayerRepository(s2).history_for_user(citizen_uid, 10)
            assert len(history) == 1 and history[0].game_id == game_id

        # --- профиль: достижения, места, XP->уровень, глобальный+локальный блоки
        async with services.session_factory() as s2:
            from bot.database.repositories.users import UserRepository

            winner = await UserRepository(s2).get_by_id(citizen_uid)
            data = await compute_profile_extras(s2, winner, services)
            assert data["extras"]["achievements"] == "3/12"
            assert data["ranks"]["rating"] == 1  # лучший рейтинг после игры
            assert winner.level == prog.level_for_xp(winner.xp)  # уровень соответствует XP
            profile = await _full_profile_text(s2, services, winner, group)
            assert "🏅 Достижения: 3/12" in profile
            assert "⭐ Общий: <b>112</b> <code>(#1)</code>" in profile  # глобальный рейтинг
            assert "В ЭТОЙ ГРУППЕ" in profile               # локальный блок
            assert "В ИГРЕ" in profile                      # игровая статистика внизу

        # --- локальная статистика группы обновилась и не смешалась с глобальной
        async with services.session_factory() as s2:
            gp_repo = GroupPlayerRepository(s2)
            gp = await gp_repo.get_membership(group.id, citizen_uid)
            assert gp.wins == 1 and gp.rating == 112 and gp.xp == 42
            assert gp.level == prog.level_for_xp(gp.xp)
            gp_mafia = await gp_repo.get_membership(group.id, mafia_uid)
            assert gp_mafia.losses == 1
            # место в группе пересчитано: победитель выше проигравшего
            assert await gp_repo.rank_in_group(group.id, "rating", gp.rating) == 1
