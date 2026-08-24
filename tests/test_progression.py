"""XP, уровни и глобальная статистика: старт с нуля, пороги, формулы RatingService.

Требования спека:
- новый игрок: rating 0, wins 0, XP 0, level 1 (НЕ 1000);
- пороги уровней 0/100/250/450/700 XP, формула меняется константами;
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
    def test_default_thresholds(self):
        p = ProgressionService()
        assert [p.threshold(l) for l in range(1, 6)] == [0, 100, 250, 450, 700]

    def test_level_for_xp_boundaries(self):
        p = ProgressionService()
        assert p.level_for_xp(0) == 1
        assert p.level_for_xp(99) == 1
        assert p.level_for_xp(100) == 2
        assert p.level_for_xp(249) == 2
        assert p.level_for_xp(250) == 3
        assert p.level_for_xp(450) == 4
        assert p.level_for_xp(699) == 4
        assert p.level_for_xp(700) == 5
        assert p.level_for_xp(1000) == 6  # прогрессия продолжается дальше 5-го

    def test_formula_easy_to_change(self):
        # пороги меняются двумя константами: step/growth
        p = ProgressionService(step=50, growth=0)
        assert [p.threshold(l) for l in range(1, 5)] == [0, 50, 100, 150]

    def test_xp_progress_in_level(self):
        p = ProgressionService()
        level, inside, need = p.xp_progress_in_level(120)
        # уровень 2, внутри уровня 120-100=20, ширина уровня 250-100=150
        assert (level, inside, need) == (2, 20, 150)


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
        events = StatEvents(correct_votes={6: 10})  # +20 XP -> 110 -> уровень 2
        service.apply_global(
            {6: row}, winners=set(), is_draw=False, events=events,
            survived_ids=set(), flags=ScopeFlags(rating=True, xp=True),
        )
        assert row.xp == 110  # 80 + участие 10 + голоса 20
        assert row.level == 2

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
