"""Тесты DEBUG MODE (TestGameManager): создание, авто-действия, изоляция."""

from __future__ import annotations

import asyncio


from bot.database.models import Game, GameStatus, User
from bot.database.repositories.actions import GameActionRepository
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.rooms import RoomRepository
from bot.database.repositories.users import UserRepository
from bot.database.repositories.votes import VoteRepository
from bot.roles import Team, get_role
from tests.conftest import make_user


async def _game(services, game_id: int) -> Game:
    async with services.session_factory() as s:
        return await GameRepository(s).get(game_id)


class TestCreate:
    async def test_create_test_game(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, message = await services.test_games.create_test_game(admin.id, 5)
        assert game_id is not None, message

        game = await _game(services, game_id)
        assert game.get_setting("test_mode") is True          # флаг тестовой игры
        assert game.get_setting("night_seconds") == 6          # ускоренные таймеры

        async with services.session_factory() as s:
            players = await GamePlayerRepository(s).list_for_game(game_id)
        assert len(players) == 5
        bots = [p for p in players if p.user.is_test]
        real = [p for p in players if not p.user.is_test]
        assert len(bots) == 4                                  # TestPlayer1..4
        assert len(real) == 1 and real[0].user_id == admin.id  # один админ
        # telegram_id ботов отрицательные и убывают с индексом -> сортируем по убыванию
        for index, bot in enumerate(sorted(bots, key=lambda p: p.user.telegram_id, reverse=True), start=1):
            assert bot.user.display_name == f"TestPlayer{index}"
            assert bot.user.telegram_id < 0  # отрицательные TG-ID
        # Роли распределены автоматически
        assert all(p.role for p in players)

        # Комната приватная и в статусе PLAYING
        async with services.session_factory() as s:
            room = await RoomRepository(s).get(game.room_id)
        assert room.is_private is True
        assert room.status == "PLAYING"
        assert room.game_id == game_id

        # Ботам отправлены карточки ролей (FakeNotifier их пишет в sent)
        assert any("ТВОЯ РОЛЬ" in text for _, text, _ in services.notifier.sent)

    async def test_create_wrong_count(self, services, session):
        admin = await make_user(session, "Admin")
        for count in (2, 3, 9, 0):
            game_id, message = await services.test_games.create_test_game(admin.id, count)
            assert game_id is None
            assert "от 4 до 8" in message

    async def test_reuse_bots_between_test_games(self, services, session):
        admin = await make_user(session, "Admin")
        first_id, _ = await services.test_games.create_test_game(admin.id, 4)
        assert first_id is not None
        await services.test_games.finish(first_id)

        second_id, _ = await services.test_games.create_test_game(admin.id, 4)
        assert second_id is not None
        # Боты переиспользованы, а не размножены
        from sqlalchemy import select

        async with services.session_factory() as s:
            bots = (await s.execute(select(User).where(User.is_test.is_(True)))).scalars().all()
        assert len(bots) == 3

    async def test_blocked_while_active_game(self, services, session):
        admin = await make_user(session, "Admin")
        first_id, _ = await services.test_games.create_test_game(admin.id, 4)
        assert first_id is not None
        second_id, message = await services.test_games.create_test_game(admin.id, 4)
        assert second_id is None
        assert "активная игра" in message


class TestAutoActions:
    async def test_auto_night_actions_bots_only(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 6)
        await services.phases.begin_game(game_id)  # STARTING -> NIGHT

        submitted = await services.test_games.auto_night_actions(game_id)
        async with services.session_factory() as s:
            players = await GamePlayerRepository(s).list_for_game(game_id)
            actions = await GameActionRepository(s).night_actions(game_id, 1)

        bot_ids = {p.user_id for p in players if p.user.is_test}
        admin_id = next(p.user_id for p in players if not p.user.is_test)
        # Действия только от ботов с ночными ролями
        assert submitted == len(actions)
        assert all(a.actor_id in bot_ids for a in actions)
        assert all(a.actor_id != admin_id for a in actions)
        # Каждое действие соответствует роли исполнителя
        for action in actions:
            actor = next(p for p in players if p.user_id == action.actor_id)
            role = get_role(actor.role)
            assert role.night_action is not None
            assert role.night_action.value == action.action_type
            target = next(p for p in players if p.user_id == action.target_id)
            if action.action_type == "kill":
                assert get_role(target.role).team != Team.MAFIA or role.team == Team.NEUTRAL

    async def test_auto_night_actions_rejects_normal_game(self, services, session):
        from tests.test_game_flow import _start_game

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(services, session, users)
        await services.phases.begin_game(game_id)
        # Обычная игра — авто-действия не выполняются
        assert await services.test_games.auto_night_actions(game_id) == 0

    async def test_auto_vote(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 6)
        await services.phases.begin_game(game_id)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)

        submitted = await services.test_games.auto_vote(game_id)
        async with services.session_factory() as s:
            players = await GamePlayerRepository(s).list_for_game(game_id)
            votes = await VoteRepository(s).round_votes(game_id, 1, 1)

        alive_bots = [p for p in players if p.user.is_test and p.is_alive]
        assert submitted == len(votes) == len(alive_bots)
        for vote in votes:
            voter = next(p for p in players if p.user_id == vote.voter_id)
            assert voter.user.is_test          # админ не голосует автоматически
            assert vote.target_id != vote.voter_id
            target = next(p for p in players if p.user_id == vote.target_id)
            assert target.is_alive             # только за живых


class TestSupervisor:
    async def test_supervisor_acts_automatically(self, services, session):
        admin = await make_user(session, "Admin")
        # Супервизор с крошечным интервалом; за админа тоже играет бот-логика
        game_id, _ = await services.test_games.create_test_game(
            admin.id, 6, supervisor_interval=0.05, auto_include_admin=True
        )
        await services.phases.begin_game(game_id)  # -> NIGHT

        # Ждём, пока супервизор выполнит ночные действия
        for _ in range(50):
            await asyncio.sleep(0.05)
            async with services.session_factory() as s:
                actions = await GameActionRepository(s).night_actions(game_id, 1)
            if actions:
                break
        assert actions, "супервизор не выполнил ночные действия"

        state = services.test_games.toggle_auto(game_id)
        assert state is False  # выключили
        services.test_games.toggle_auto(game_id)  # обратно включили

    async def test_full_cycle_to_end_with_bots(self, services, session):
        """Полный игровой цикл: боты играют сами, фазы завершаем вручную
        (в тестах таймеры Noop). Игра обязана дойти до ENDED."""
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(
            admin.id, 6, supervisor_interval=0.05, auto_include_admin=True
        )

        await services.phases.begin_game(game_id)
        steps = 0
        while steps < 60:
            steps += 1
            game = await _game(services, game_id)
            if game.status == GameStatus.ENDED.value:
                break
            if game.status == GameStatus.NIGHT.value:
                await services.test_games.skip_phase(game_id)
            elif game.status == GameStatus.DAY.value:
                await services.test_games.skip_phase(game_id)
            elif game.status == GameStatus.VOTING.value:
                # даём супервизору время проголосовать за ботов
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    async with services.session_factory() as s:
                        votes = await VoteRepository(s).round_votes(
                            game_id, game.day_number,
                            int((game.vote_context or {}).get("round_no", 1)),
                        )
                    alive_bots = None
                    async with services.session_factory() as s:
                        players = await GamePlayerRepository(s).list_for_game(game_id)
                        alive = [p for p in players if p.is_alive and p.user.is_test]
                    if len(votes) >= len(alive):
                        break
                await services.test_games.skip_phase(game_id)
            await asyncio.sleep(0.05)

        game = await _game(services, game_id)
        assert game.status == GameStatus.ENDED.value, f"не завершилась: {game.status}, день {game.day_number}"
        assert game.winner in ("city", "mafia", "maniac", "draw")

        # Тестовая игра не меняет статистику реального админа
        async with services.session_factory() as s:
            fresh_admin = await UserRepository(s).get_by_id(admin.id)
        assert fresh_admin.games_played == 0
        assert fresh_admin.rating == 1000
        assert fresh_admin.wins == 0


class TestControls:
    async def test_skip_phase(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5)
        await services.phases.begin_game(game_id)

        result = await services.test_games.skip_phase(game_id)  # NIGHT -> DAY
        assert "Ночь" in result and "День" in result
        assert (await _game(services, game_id)).status == GameStatus.DAY.value

    async def test_skip_phase_rejects_normal_game(self, services, session):
        from tests.test_game_flow import _start_game

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(services, session, users)
        result = await services.test_games.skip_phase(game_id)
        assert "не тестовая" in result

    async def test_finish(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5)
        await services.phases.begin_game(game_id)

        result = await services.test_games.finish(game_id)
        assert "завершена" in result
        game = await _game(services, game_id)
        assert game.status == GameStatus.ENDED.value
        assert game.winner == "draw"

        # Повторное завершение — честный отказ
        assert "уже завершена" in await services.test_games.finish(game_id)

    async def test_act_now(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5)
        await services.phases.begin_game(game_id)
        result = await services.test_games.act_now(game_id)
        assert "Ночные действия" in result and "0" not in result.split(":")[-1].strip(".")

    async def test_dump_state(self, services, session, caplog):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 5)

        text = await services.test_games.dump_state(game_id)
        assert f"#{game_id}" in text
        assert "TestPlayer1" in text
        assert "Подготовка" in text or "STARTING" in text  # статус указан
        # Роли видны в дампе (это отладочный режим)
        assert "МАФИЯ" in text or "КОМИССАР" in text or "ДОКТОР" in text or "МИРНЫЙ" in text

        # Дамп попадает в консоль (logger)
        with caplog.at_level("INFO", logger="bot.services.test_game"):
            await services.test_games.dump_state(game_id)
        assert any("Состояние тест-игры" in rec.message for rec in caplog.records)


class TestIsolation:
    async def test_normal_game_stats_still_applied(self, services, session):
        """Обычная (не тестовая) игра по-прежнему меняет статистику."""
        from tests.test_game_flow import _start_game, _roles_map

        users = [await make_user(session, f"P{i}") for i in range(1, 7)]
        game_id = await _start_game(
            services, session, users, roles_setup={"mafia": 1, "detective": 1, "doctor": 1}
        )
        roles = await _roles_map(services, game_id)
        mafia_uid = next(uid for uid, r in roles.items() if r == "mafia")

        await services.phases.begin_game(game_id)
        citizen_uid = next(uid for uid, r in roles.items() if r == "citizen")
        await services.games.submit_night_action(game_id, mafia_uid, "kill", citizen_uid)
        await services.phases.end_night(game_id)
        await services.phases.begin_voting(game_id)
        for uid in roles:
            if uid != mafia_uid:
                await services.games.cast_vote(game_id, uid, mafia_uid)
        await services.phases.end_voting(game_id)

        game = await _game(services, game_id)
        assert game.status == GameStatus.ENDED.value
        async with services.session_factory() as s:
            mafia_user = await UserRepository(s).get_by_id(mafia_uid)
        assert mafia_user.games_played == 1
        assert mafia_user.losses == 1

    async def test_bots_excluded_from_rating_and_broadcast(self, services, session):
        admin = await make_user(session, "Admin")
        game_id, _ = await services.test_games.create_test_game(admin.id, 4)
        await services.test_games.finish(game_id)

        async with services.session_factory() as s:
            users_repo = UserRepository(s)
            top = await users_repo.top_by_rating(10)
            broadcast_ids = await users_repo.ids_for_broadcast()
            total = await users_repo.count_all()

        assert all(not u.is_test for u in top)
        assert all(uid >= 0 for uid in broadcast_ids)
        assert total == 1  # только админ
