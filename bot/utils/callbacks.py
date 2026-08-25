"""Фабрики callback_data (aiogram 3 CallbackData).

Все колбэки несут идентификаторы сущностей (room_id / game_id / target_id),
поэтому «устаревшие» кнопки всегда проверяются по актуальному состоянию в БД.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    """Главное меню и профиль/настройки."""

    action: str  # play | profile | rating | rules | settings | ...
    value: str = ""


class RoomCB(CallbackData, prefix="room"):
    """Действия в комнате."""

    action: str  # view | join | leave | ready | kick | close | start | ...
    room_id: int
    value: str = ""  # дополнительный параметр (id игрока для kick и т.п.)


class RoomCreateCB(CallbackData, prefix="rcreate"):
    """Мастер создания комнаты (FSM-подсказки)."""

    action: str  # maxp | minp | privacy | roles | preset | tie | reveal | timer
    value: str = ""


class GameCB(CallbackData, prefix="game"):
    """Игровые действия вне ночи/голосования."""

    action: str  # status | leave | confirm_leave | night | cancel
    game_id: int
    value: str = ""


class NightCB(CallbackData, prefix="night"):
    """Выбор цели ночного действия."""

    game_id: int
    action: str      # kill | heal | check | block | protect
    target_id: int   # users.id


class NightConfirmCB(CallbackData, prefix="nconf"):
    """Подтверждение ночного действия."""

    game_id: int
    action: str
    target_id: int
    op: str  # yes | no


class VoteCB(CallbackData, prefix="vote"):
    """Выбор кандидата на голосовании."""

    game_id: int
    round_no: int
    target_id: int  # users.id


class VoteConfirmCB(CallbackData, prefix="vconf"):
    """Подтверждение голоса."""

    game_id: int
    round_no: int
    target_id: int
    op: str  # yes | no


class AdminCB(CallbackData, prefix="admin"):
    """Админ-панель."""

    action: str  # panel | stats | games | rooms | logs | roles | settings | ...
    value: str = ""


class ProfileCB(CallbackData, prefix="prof"):
    """Настройки профиля."""

    action: str  # name | games | back
    value: str = ""


class TestCB(CallbackData, prefix="test"):
    """DEBUG MODE: управление тестовой игрой."""

    action: str  # create | status | skip | actnow | auto | finish
    value: str = ""


class RatingCB(CallbackData, prefix="rate"):
    """Рейтинги: скоуп + метрика + страница."""

    scope: str   # global | local
    metric: str  # rating | wins | level
    page: int = 0


class SettingCB(CallbackData, prefix="gset"):
    """Настройки группы."""

    action: str  # menu | players | timers | roles | voting | progression | extra | set
    value: str = ""


class HistoryCB(CallbackData, prefix="hist"):
    """История игр: пагинация + детальный просмотр."""

    action: str   # page | detail
    page: int = 0
    game_id: int = 0


class SocialCB(CallbackData, prefix="soc"):
    """Социальные действия: друзья/запросы/избранные/награды."""

    action: str   # friends | requests | favorites | ignored | rewards | accept | decline | ...
    value: str = ""
