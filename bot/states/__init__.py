"""FSM-состояния."""

from aiogram.fsm.state import State, StatesGroup


class RoomCreationStates(StatesGroup):
    name = State()
    max_players = State()
    min_players = State()
    privacy = State()
    password = State()
    roles = State()  # интерактивно настраивается inline-кнопками


class JoinByIdStates(StatesGroup):
    room_id = State()


class PasswordStates(StatesGroup):
    password = State()


class ProfileStates(StatesGroup):
    display_name = State()


class OwnerStates(StatesGroup):
    """OWNER-панель (/owner): пошаговый ввод (игрок, число, награда)."""

    player_input = State()   # ввод ID/@username игрока
    value_input = State()    # ввод числа (рейтинг/победы/XP/уровень)
    reward_input = State()   # создание ивентовой награды (code|emoji|name|kind|дни|описание)


class AdminStates(StatesGroup):
    ban_input = State()
    unban_input = State()
    broadcast_input = State()
    param_input = State()


class NoteStates(StatesGroup):
    """Предсмертная записка: ожидание текста от умершего игрока."""
    text = State()


class SocialStates(StatesGroup):
    """Ввод цели для социальных команд (если аргумент не передан сразу)."""
    target = State()
