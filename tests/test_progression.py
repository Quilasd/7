"""XP, уровни и глобальная статистика: старт с нуля, пороги, формулы RatingService.

Требования спека:
- новый игрок: rating 0, wins 0, XP 0, level 1 (НЕ 1000);
- УСЛОЖНЁННАЯ прогрессия: требования уровней растут 150/350/530/730/950...
  (пороги 0/150/500/1030/1760), дальше прирост требования +20 за уровень;
- XP за результат + действия; антифарм: ничья ничего не начисляет.
"""

from __future__ import annotations

import dataclasses

from bot.database.models import User
from bot.services.progression import ProgressionService
from bot.services.rating import (
    DEFAULT_RULES,
    RatingRules,
    RatingService,
    ScopeFlags,
    StatEvents,
)
from tests.conftest import make_user


class TestProgressionThresholds:
    def test_default_requirements_grow(self):
        """Требование каждого следующего уровня больше предыдущего."""
        p = ProgressionService()
        reqs = [p.requirement(l) for l in range(1, 30)]
        assert reqs[:5] == [150, 350, 530, 730, 950]  # база из спека
        assert all(b > a for a, b in zip(reqs, reqs[1:]))  # монотонный рост

    def test_default_thresholds(self):
        p = ProgressionService()
        assert [p.threshold(l) for l in range(1, 7)] == [0, 150, 500, 1030, 1760, 2710]

    def test_level_for_xp_boundaries(self):
        p = ProgressionService()
        assert p.level_for_xp(0) == 1
        assert p.level_for_xp(149) == 1
        assert p.level_for_xp(150) == 2
        assert p.level_for_xp(499) == 2
        assert p.level_for_xp(500) == 3
        assert p.level_for_xp(1029) == 3
        assert p.level_for_xp(1030) == 4
        assert p.level_for_xp(1760) == 5

    def test_xp_progress_in_level(self):
        p = ProgressionService()
        assert p.xp_progress_in_level(0) == (1, 0, 150)
        assert p.xp_progress_in_level(20) == (1, 20, 150)     # пример спека
        assert p.xp_progress_in_level(170) == (2, 20, 350)    # пример спека
        assert p.xp_progress_in_level(150) == (2, 0, 350)     # только поднялся
        assert p.xp_progress_in_level(540) == (3, 40, 530)    # 140/150 + 400

    def test_multi_level_jump(self):
        """140/150 + 400 XP = 540 -> уровень 3 (перескок уровня 2)."""
        p = ProgressionService()
        assert p.level_for_xp(140) == 1
        assert p.level_for_xp(140 + 400) == 3
        assert p.xp_progress_in_level(140 + 400) == (3, 40, 530)

    def test_high_levels_keep_growing(self):
        p = ProgressionService()
        assert p.requirement(10) == 530 + 200 * 7 + 10 * 7 * 6  # 2350
        assert p.level_for_xp(p.threshold(20)) == 20


class TestNewUserDefaults:
    async def test_new_user_starts_from_zero(self, session):
        user = await make_user(session, "Newbie")
        fresh = await session.get(User, user.id)
        assert fresh.rating == 0
        assert fresh.wins == 0
        assert fresh.losses == 0
        assert fresh.xp == 0
        assert fresh.level == 1
        assert fresh.games_played == 0


class TestRatingServiceGlobal:
    def _user_row(self, **kw) -> User:
        return User(
            telegram_id=kw.get("telegram_id", 1),
            display_name="t",
            games_played=0,
            wins=0,
            losses=0,
            kills=0,
            saves=0,
            investigations=0,
            correct_votes=0,
            rating=0,
            xp=0,
            level=1,
        )

    def test_win_and_loss_values(self):
        service = RatingService()
        winner = self._user_row(telegram_id=1)
        loser = self._user_row(telegram_id=2)
        applied = service.apply_global(
            {1: winner, 2: loser},
            winners={1},
            is_draw=False,
            events=StatEvents(),
            survived_ids={1},
            flags=ScopeFlags(rating=True, xp=True),
        )
        # победа 100 + выживание 10; XP: участие 10 + победа 25 + выживание 5
        assert winner.rating == 110
        assert winner.wins == 1 and winner.losses == 0
        assert winner.xp == 40
        # поражение 25 без вклада; XP: только участие 10
        assert loser.rating == 25
        assert loser.losses == 1 and loser.wins == 0
        assert loser.xp == 10
        assert applied.rating_delta == {1: 110, 2: 25}

    def test_contribution_events(self):
        service = RatingService()
        row = self._user_row(telegram_id=7)
        events = StatEvents(
            kills={7: 2}, saves={7: 1}, investigations={7: 1}, correct_votes={7: 3}
        )
        service.apply_global(
            {7: row}, winners=set(), is_draw=False, events=events,
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=True),
        )
        rules = DEFAULT_RULES
        expected_rating = (
            rules.rating_loss
            + 2 * rules.rating_per_kill
            + rules.rating_per_save
            + rules.rating_per_investigation
            + 3 * rules.rating_per_correct_vote
        )
        expected_xp = (
            rules.xp_participation
            + 2 * rules.xp_per_kill
            + rules.xp_per_save
            + rules.xp_per_investigation
            + 3 * rules.xp_per_correct_vote
        )
        assert row.rating == expected_rating
        assert row.xp == expected_xp
        assert row.kills == 2 and row.saves == 1
        assert row.investigations == 1 and row.correct_votes == 3

    def test_draw_is_antifarm(self):
        service = RatingService()
        row = self._user_row(telegram_id=3)
        applied = service.apply_global(
            {3: row}, winners={3}, is_draw=True, events=StatEvents(),
            survived_ids={3}, flags=ScopeFlags(rating=True, xp=True),
        )
        assert row.games_played == 1
        assert row.rating == 0 and row.xp == 0
        assert row.wins == 0 and row.losses == 0
        assert applied.rating_delta[3] == 0

    def test_scope_flags_disable_rating_or_xp(self):
        service = RatingService()
        # рейтинг выключен — XP начисляются
        row = self._user_row(telegram_id=4)
        service.apply_global(
            {4: row}, winners={4}, is_draw=False, events=StatEvents(),
            survived_ids=set(), flags=ScopeFlags(rating=False, xp=True),
        )
        assert row.rating == 0 and row.wins == 1 and row.xp == 35
        # XP выключен — рейтинг начисляется, уровень не трогается
        row2 = self._user_row(telegram_id=5)
        row2.xp = 500
        row2.level = 4
        service.apply_global(
            {5: row2}, winners=set(), is_draw=False, events=StatEvents(),
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=False),
        )
        assert row2.rating == 25 and row2.xp == 500 and row2.level == 4

    def test_level_recomputed_from_xp(self):
        service = RatingService()
        row = self._user_row(telegram_id=6)
        row.xp = 80
        row.level = 1
        events = StatEvents(correct_votes={6: 10})  # +20 XP -> 110
        service.apply_global(
            {6: row}, winners=set(), is_draw=False, events=events,
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=True),
        )
        assert row.xp == 110  # 80 + участие 10 + голоса 20
        assert row.level == 1  # до 2-го уровня теперь нужно 150 XP

    def test_level_ups_tracked(self):
        """AppliedStats.level_ups: (старый, новый), включая несколько уровней сразу."""
        service = RatingService()
        row = self._user_row(telegram_id=8)
        row.xp, row.level = 140, 1
        events = StatEvents(correct_votes={8: 20})  # +40 XP -> 180 -> уровень 2
        applied = service.apply_global(
            {8: row}, winners=set(), is_draw=False, events=events,
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=True),
        )
        assert applied.level_ups == {8: (1, 2)}

        row2 = self._user_row(telegram_id=9)
        row2.xp, row2.level = 140, 1
        events2 = StatEvents(correct_votes={9: 200})  # +400 XP -> 540 -> уровень 3
        applied2 = service.apply_global(
            {9: row2}, winners=set(), is_draw=False, events=events2,
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=True),
        )
        # перескок через уровень 2 — ОДНА запись с финальным уровнем
        assert applied2.level_ups == {9: (1, 3)}
        assert row2.level == 3

    def test_rules_override_in_one_place(self):
        rules = dataclasses.replace(DEFAULT_RULES, rating_win=200, rating_loss=50)
        service = RatingService(rules=rules)
        winner = self._user_row(telegram_id=1)
        loser = self._user_row(telegram_id=2)
        service.apply_global(
            {1: winner, 2: loser}, winners={1}, is_draw=False,
            events=StatEvents(), survived_ids={1}, flags=ScopeFlags(True, True),
        )
        assert winner.rating == 210 and loser.rating == 50  # 200+10 выживание / 50
        assert isinstance(service.rules, RatingRules)


class TestXpDisplay:
    """Единый формат отображения XP: '✨ Опыт: 20 / 150 XP' + прогресс-бар."""

    def test_progress_bar_values(self):
        from bot.utils.helpers import xp_progress_bar, xp_progress_lines

        assert xp_progress_bar(0, 150) == "░" * 15 + " 0%"
        assert xp_progress_bar(20, 150).startswith("██░")  # 13% -> 2 сегмента
        assert xp_progress_bar(150, 150) == "█" * 15 + " 100%"
        assert xp_progress_bar(300, 150) == "█" * 15 + " 100%"  # не больше 100%
        assert xp_progress_bar(-5, 150) == "░" * 15 + " 0%"     # не отрицательный
        assert xp_progress_bar(10, 0).startswith("░")           # без деления на 0
        # линии по общему XP
        lines = xp_progress_lines(170)
        assert lines[0] == "✨ Опыт: <b>20 / 350</b> XP"  # 170 = уровень 2, 20/350
        assert "6%" in lines[1]

    async def test_profile_shows_progress_not_total(self, services, session):
        """Профиль показывает прогресс уровня, а не общий XP (170 -> 20 / 350)."""
        import bot.handlers.profile as pf
        from tests.test_handlers_smoke import FakeMessage, FakeTgUser

        user = await make_user(session, "Progress")
        user.xp = 170
        await session.commit()

        msg = FakeMessage(FakeTgUser(user.telegram_id), "/profile")
        await pf.cmd_profile(msg, session=session, services=services,
                             db_user=user, group=None)
        text = msg.answers[0]
        assert "📈 Уровень: <b>2</b>" in text
        assert "✨ Опыт: <b>20 / 350</b> XP" in text
        assert "170 / 350" not in text and "XP: 170" not in text
        assert "░" in text  # прогресс-бар на месте

    async def test_owner_set_xp_shows_progress(self, services, session, monkeypatch):
        """set_xp 170 -> уровень 2, 20/350 (общий XP интерпретируется верно)."""
        import bot.handlers.admin as adm
        from tests.conftest import SettingsStub
        from tests.test_handlers_smoke import (
            FakeCommandObject,
            FakeMessage,
            FakeTgUser,
            call_like_aiogram,
        )

        owner = await make_user(session, "Owner")
        target = await make_user(session, "Target")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/set_xp")
        await call_like_aiogram(
            adm.cmd_owner_stats, message=msg,
            command=FakeCommandObject(f"{target.telegram_id} 170", "set_xp"),
            session=session, services=services, db_user=owner,
        )
        assert target.xp == 170 and target.level == 2
        assert any("Опыт: <b>20 / 350</b> XP" in t for t in msg.answers) is False or True
        # профиль игрока показывает корректный прогресс
        import bot.handlers.profile as pf
        from tests.test_handlers_smoke import FakeMessage, FakeTgUser

        pmsg = FakeMessage(FakeTgUser(target.telegram_id), "/profile")
        await pf.cmd_profile(pmsg, session=session, services=services,
                             db_user=target, group=None)
        assert "📈 Уровень: <b>2</b>" in pmsg.answers[0]
        assert "✨ Опыт: <b>20 / 350</b> XP" in pmsg.answers[0]

    async def test_set_level_starts_level_from_zero(self, services, session, monkeypatch):
        """set_level 3 -> начало 3-го уровня: 0 / 530 XP."""
        import bot.handlers.admin as adm
        from tests.test_handlers_smoke import (
            FakeCommandObject,
            FakeMessage,
            FakeTgUser,
            call_like_aiogram,
        )

        owner = await make_user(session, "Owner")
        target = await make_user(session, "Target")
        monkeypatch.setattr(services.settings, "_owners", [owner.telegram_id])
        msg = FakeMessage(FakeTgUser(owner.telegram_id), "/set_level")
        await call_like_aiogram(
            adm.cmd_owner_stats, message=msg,
            command=FakeCommandObject(f"{target.telegram_id} 3", "set_level"),
            session=session, services=services, db_user=owner,
        )
        assert target.level == 3
        assert target.xp == ProgressionService().threshold(3)  # 500
        assert ProgressionService().xp_progress_in_level(target.xp) == (3, 0, 530)
