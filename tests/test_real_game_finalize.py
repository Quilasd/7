"""РЕАЛЬНЫЙ game flow с настоящими таймерами (регрессия финализации игры).

Баг: PhaseManager._end_game() вызывал timers.cancel(game.id) из САМОЙ таймерной
задачи фазы. TimerManager.cancel отменял ВСЕ задачи игры, включая текущую —
на первом же await внутри _end_game взлетал CancelledError, и весь финальный
pipeline (статистика/XP/рейтинг/серии/достижения/сообщения) молча умирал:
игра получала статус ENDED, но игроки не получали ничего. В test_game и pytest
этого не было видно: там фазы вызываются напрямую, без таймерных задач.

Здесь сервисы собираются с НАСТОЯЩИМ TimerManager (как в bot.main.build_services),
фазы двигают только таймеры — тест ловит регрессию самоотмены.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from bot.database.models import GameStatus
from bot.database.repositories.games import GamePlayerRepository, GameRepository
from bot.database.repositories.users import UserRepository
from bot.services.game_manager import GameManager
from bot.services.phase_manager import GameLocks, PhaseManager
from bot.services.rating import RatingService
from bot.services.timer_manager import TimerManager
from tests.conftest import make_room, make_ready, make_user

# короткие фазы, чтобы тест шёл секунды
FAST_SETTINGS = {
    "night_seconds": 1,
    "day_seconds": 1,
    "vote_seconds": 1,
    "start_countdown_seconds": 1,
    "tie_rule": "revote",
    "reveal_roles_on_death": True,
}


@pytest_asyncio.fixture()
async def live_services(session_factory, notifier):
    """Контейнер как в проде: НАСТОЯЩИЙ TimerManager (фазы двигают таймеры)."""
    timers = TimerManager()
    locks = GameLocks()
    phases = PhaseManager(
        session_factory, notifier, timers, locks,
        rating=RatingService(), app_settings=None,
    )
    games = GameManager(session_factory, notifier, phases, locks)
    container = type("LiveServices", (), {})()
    container.session_factory = session_factory
    container.notifier = notifier
    container.timers = timers
    container.phases = phases
    container.games = games
    yield container
    timers.cancel_all()


async def _play_until_ended(live_services, game_id: int, *, night_kill: bool,
                            lynch_mafia: bool, timeout_s: float = 30.0) -> None:
    """Супервизор реальных игроков: ночью киллит мафия, днём линчуют цель."""
    sf = live_services.session_factory
    deadline = asyncio.get_event_loop().time() + timeout_s
    acted_nights: set[int] = set()
    voted_rounds: set[tuple[int, int]] = set()

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
        async with sf() as s:
            game = await GameRepository(s).get(game_id)
            if game is None:
                return
            if game.status == GameStatus.ENDED.value:
                # даём финальной обработке договорить (рассылка после commit)
                await asyncio.sleep(0.3)
                return
            players = await GamePlayerRepository(s).list_for_game(game_id)
            roles = {p.user_id: p.role for p in players}
            alive = [p.user_id for p in players if p.is_alive]
        mafia_uids = [u for u, r in roles.items() if r == "mafia"]

        if game.status == GameStatus.NIGHT.value and night_kill \
                and game.day_number not in acted_nights and mafia_uids:
            mafia = mafia_uids[0]
            victims = [u for u in alive if u not in mafia_uids]
            if victims:
                res = await live_services.games.submit_night_action(
                    game_id, mafia, "kill", victims[0]
                )
                if res.ok:
                    acted_nights.add(game.day_number)

        elif game.status == GameStatus.VOTING.value:
            key = (game.day_number, game.vote_context.get("round_no", 1)
                   if game.vote_context else 1)
            if key in voted_rounds:
                continue
            target = None
            if lynch_mafia and mafia_uids:
                target = next((u for u in mafia_uids if u in alive), None)
            if target is None:
                target = next((u for u in alive if u not in mafia_uids), None)
            if target is None:
                continue
            ok_any = False
            for uid in alive:
                if uid != target:
                    res = await live_services.games.cast_vote(game_id, uid, target)
                    ok_any = ok_any or res.ok
            if ok_any:
                voted_rounds.add(key)

    pytest.fail(f"Игра {game_id} не завершилась за {timeout_s}с")


class TestRealGameFinalizesViaTimers:
    """Реальная игра (не test_game): финальный pipeline обязан отработать."""

    async def test_city_win_full_finalization(self, live_services, session, notifier):
        """Город линчует мафию: победа, XP, уровень, рейтинг, серия, верные
        голоса, игры, достижения, история и сообщение о завершении."""
        users = [await make_user(session, f"R{i}") for i in range(1, 5)]
        room = await make_room(
            session, users[0], users,
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )
        room.settings = dict(room.settings or {}, **FAST_SETTINGS)
        for user in users:
            await make_ready(session, room, user)
        await session.commit()

        res = await live_services.games.start_game_from_room(room.id, users[0].id)
        assert res.ok, res.message
        async with live_services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = (await RoomRepository(s).get(room.id)).game_id

        # реальная игра: мафия ночью убивает, город линчует мафию
        await _play_until_ended(live_services, game_id, night_kill=True, lynch_mafia=True)

        async with live_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "city"
            assert game.ended_at is not None
            # таймлайн сохранён (для /game_<ID>)
            assert any(e.get("type") == "lynch" for e in (game.events or []))

            players = await GamePlayerRepository(s).list_for_game(game_id)
            roles = {p.user_id: p.role for p in players}
            mafia_uid = next(u for u, r in roles.items() if r == "mafia")
            city_uids = [u for u, r in roles.items() if r != "mafia"]
            # ночная жертва (не в победителях, но статистика партии на месте)
            night_victim = next(
                (p.user_id for p in players if p.death_cause == "mafia"), None
            )

            fresh = {u.id: await UserRepository(s).get_by_id(u.id) for u in users}
            for uid in city_uids:
                f = fresh[uid]
                survived = uid != night_victim
                assert f.games_played == 1, "🎮 игры не зачислены"
                assert f.wins == 1, "🏆 победа не зачислена"
                assert f.losses == 0
                if survived:
                    # выживший: 10 участие + 25 победа + 2 верный голос + 5 выживание
                    assert f.xp == 42, f"XP выжившего: {f.xp}"
                    assert f.rating == 112, f"рейтинг выжившего: {f.rating}"
                    assert f.correct_votes == 1, "🗳 верный голос не засчитан"
                else:
                    # ночная жертва: без выживания, голосовать не могла
                    assert f.xp == 35, f"XP жертвы: {f.xp}"
                    assert f.rating == 100, f"рейтинг жертвы: {f.rating}"
                    assert f.correct_votes == 0
                assert f.level == 1  # до 150 XP — уровень 1
                assert f.win_streak == 1 and f.best_win_streak == 1, "🔥 серия не обновлена"
            m = fresh[mafia_uid]
            assert m.games_played == 1
            assert m.wins == 0 and m.losses == 1, "💀 поражение не зачислено"
            assert m.xp == 15, f"XP мафии: {m.xp}"  # 10 участие + 5 килл
            assert m.rating == 30  # 25 поражение + 5 за килл
            assert m.win_streak == 0 and m.best_win_streak == 0
            assert m.correct_votes == 0

            # достижения оценились ПОСЛЕ статистики (first_win/city_win/sharp_eye)
            from bot.services.achievements import get_achievement

            history = await GamePlayerRepository(s).history_for_user(city_uids[0], limit=5)
            assert len(history) == 1 and history[0].game_id == game_id, (
                "📜 история не сохранена"
            )

        # финальное сообщение о завершении — каждому живому/участнику
        for user in users:
            assert notifier.contains(user.telegram_id, "ИГРА ОКОНЧЕНА"), (
                f"игрок {user.id} не получил сообщение о завершении"
            )
        # у победителей-горожан — достижения первой победы
        winners_msgs = [
            t for uid, t, _ in notifier.sent
            if "ИГРА ОКОНЧЕНА" in t and "достижение" in t.lower()
        ]
        assert winners_msgs, "достижения не доставлены в финальном сообщении"
        assert any("Первая кровь" in t for t in winners_msgs)
        assert get_achievement("city_win") is not None

    async def test_mafia_wins_at_night_via_timers(self, live_services, session, notifier):
        """Победа мафии утром после ночи (путь end_night → _end_game из таймера)."""
        users = [await make_user(session, f"M{i}") for i in range(1, 5)]
        room = await make_room(
            session, users[0], users,
            roles_setup={"mafia": 2, "detective": 1, "doctor": 0},
        )
        room.settings = dict(room.settings or {}, **FAST_SETTINGS)
        for user in users:
            await make_ready(session, room, user)
        await session.commit()

        res = await live_services.games.start_game_from_room(room.id, users[0].id)
        assert res.ok, res.message
        async with live_services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = (await RoomRepository(s).get(room.id)).game_id

        # 2 мафии убивают 1 горожанина ночью -> 2 мафии vs 1 город -> победа мафии
        await _play_until_ended(
            live_services, game_id, night_kill=True, lynch_mafia=False
        )

        async with live_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            assert game.status == GameStatus.ENDED.value
            assert game.winner == "mafia"

            players = await GamePlayerRepository(s).list_for_game(game_id)
            roles = {p.user_id: p.role for p in players}
            mafia_uids = [u for u, r in roles.items() if r == "mafia"]
            city_uids = [u for u, r in roles.items() if r != "mafia"]

            fresh = {u.id: await UserRepository(s).get_by_id(u.id) for u in users}
            # кто из мафии убивал — по событию ночного килла
            night_kill_event = next(
                (e for e in (game.events or []) if e.get("type") == "death"
                 and e.get("cause") == "mafia"), {}
            )
            mafia_killer = next(iter(night_kill_event.get("killers", [])), None)
            for uid in mafia_uids:
                f = fresh[uid]
                assert f.wins == 1 and f.losses == 0
                expected = 45 if uid == mafia_killer else 40  # +5 за килл
                assert f.xp == expected, f"XP мафии {uid}: {f.xp}"
                assert f.win_streak == 1
            for uid in city_uids:
                f = fresh[uid]
                assert f.wins == 0 and f.losses == 1
                assert f.xp >= 10

        for user in users:
            assert notifier.contains(user.telegram_id, "ИГРА ОКОНЧЕНА")


class TestDoubleFinalIdempotent:
    """Двойной вызов финала не должен задваивать награды (ТЗ-13)."""

    async def test_force_end_after_finish_is_noop(self, live_services, session):
        users = [await make_user(session, f"D{i}") for i in range(1, 5)]
        room = await make_room(
            session, users[0], users,
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )
        room.settings = dict(room.settings or {}, **FAST_SETTINGS)
        for user in users:
            await make_ready(session, room, user)
        await session.commit()

        res = await live_services.games.start_game_from_room(room.id, users[0].id)
        assert res.ok
        async with live_services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = (await RoomRepository(s).get(room.id)).game_id

        await _play_until_ended(live_services, game_id, night_kill=True, lynch_mafia=True)

        async with live_services.session_factory() as s:
            users_before = {
                u.id: (await UserRepository(s).get_by_id(u.id)).xp
                for u in users
            }

        # повторный финал: force_end отклонён (игра уже ENDED)
        assert await live_services.phases.force_end(game_id, "повтор") is False
        # и «просроченный» таймер фазы безопасен (статус уже не VOTING/NIGHT)
        await live_services.phases.end_voting(game_id)
        await live_services.phases.end_night(game_id)

        async with live_services.session_factory() as s:
            for u in users:
                f = await UserRepository(s).get_by_id(u.id)
                assert f.xp == users_before[u.id], "XP задвоился при повторном финале"
                assert f.games_played == 1


class TestHistoryAfterRealGame:
    """/history и /game_<ID> показывают завершённую реальную игру (ТЗ-10)."""

    async def test_history_and_game_detail_after_timers_game(
        self, live_services, session, notifier
    ):
        import bot.handlers.history as hist
        from tests.test_handlers_smoke import FakeMessage, FakeTgUser

        users = [await make_user(session, f"H{i}") for i in range(1, 5)]
        room = await make_room(
            session, users[0], users,
            roles_setup={"mafia": 1, "detective": 1, "doctor": 1},
        )
        room.settings = dict(room.settings or {}, **FAST_SETTINGS)
        for user in users:
            await make_ready(session, room, user)
        await session.commit()

        res = await live_services.games.start_game_from_room(room.id, users[0].id)
        assert res.ok
        async with live_services.session_factory() as s:
            from bot.database.repositories.rooms import RoomRepository

            game_id = (await RoomRepository(s).get(room.id)).game_id

        await _play_until_ended(live_services, game_id, night_kill=True, lynch_mafia=True)

        # /history: игра в списке
        winner_user = users[0]
        async with live_services.session_factory() as s:
            for u in users:
                if (await UserRepository(s).get_by_id(u.id)).wins == 1:
                    winner_user = u
                    break
        msg = FakeMessage(FakeTgUser(winner_user.telegram_id), "/history")
        async with live_services.session_factory() as s:
            await hist.cmd_history(msg, session=s, db_user=winner_user)
        text = msg.answers[0]
        assert "ИСТОРИЯ ИГР" in text and "всего 1" in text
        assert f"Игра #{game_id}" in text
        assert f"/game_{game_id}" in text

        # /game_<ID>: состав, роли, длительность, исход, таймлайн
        # (карточка собирается _detail_text; хендлер-обёртка проверена смоком ниже)
        async with live_services.session_factory() as s:
            game = await GameRepository(s).get(game_id)
            players = await GamePlayerRepository(s).list_for_game(game_id)
            detail = hist._detail_text(game, players)
        assert f"#{game_id}" in detail
        assert "Исход" in detail and "Город" in detail  # исход партии
        assert "Длительность" in detail
        assert "Состав" in detail
        assert "Хронология" in detail  # таймлайн
        # роли всех игроков в карточке
        from bot.roles import get_role

        for p in players:
            role = get_role(p.role)
            if role:
                assert role.title in detail
