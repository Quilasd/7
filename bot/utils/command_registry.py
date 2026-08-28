"""Единый реестр команд Mafia Online (для справки /cmdhelp и /acmdhelp).

Единственный источник данных о командах для системы справки:
- CommandMeta: имя, описание, категория, permission/уровень, область действия;
- PLAYER_COMMANDS — команды обычных игроков (видимы в /cmdhelp);
- ADMIN_COMMANDS — административные команды (видимы в /acmdhelp по уровню).

РЕАЛЬНЫЕ ПРАВА: реестр не создаёт новую систему permissions — он ссылается
на существующую (PermissionService / LEVEL_PERMISSIONS). Порог уровня
команды вычисляется ИЗ LEVEL_PERMISSIONS (минимальный уровень, имеющий
право), поэтому список в /acmdhelp всегда соответствует реальным проверкам
хендлеров. Справка — только отображение: каждый хендлер продолжает
самостоятельно проверять права (без доверия к справке).

Области действия (scope):
- "local"  — проверяется в конкретной группе (локальный Lv.1–Lv.4);
- "global" — только глобальная администрация (.env OWNER_IDS/ADMIN_IDS);
- "any"    — и локально, и глобально (уровень решает).
Отдельно available_in_group/private — где команда работает.

Multi-server: локальные уровни всегда относятся к КОНКРЕТНОМУ chat_id;
реестр ничего не хранит и не решает — только описывает.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.services.permissions import AdminLevel, LEVEL_PERMISSIONS, Permission


@dataclass(frozen=True)
class CommandMeta:
    command: str
    description: str
    category: str                      # ключ CATEGORY_TITLES
    permission: Permission | None = None  # существующее право (локальная проверка)
    level: int = 0                     # прямой порог, если permission нет
    scope: str = "local"               # local | global | any
    in_group: bool = True              # работает в группе
    in_private: bool = True            # работает в ЛС
    visible_to_users: bool = False     # показывать в /cmdhelp
    note: str = ""                     # примечание к команде
    # Telegram-алиасы той же команды (один handler, несколько имён):
    # в справке показывается основное имя, инвариант полноты реестра
    # сверяет их все (tests/test_command_registry.py).
    aliases: tuple[str, ...] = ()


# ---------------------------------------------------------------- категории

PLAYER_CATEGORY_TITLES = [
    ("main", "🧭 МЕНЮ"),
    ("profile", "👤 ПРОФИЛЬ И РЕЙТИНГИ"),
    ("badges", "🎓 ТИТУЛЫ И НАГРАДЫ"),
    ("social", "👥 ДРУЗЬЯ И ИГРОКИ"),
    ("rooms", "🎮 ИГРА"),
]

ADMIN_CATEGORY_TITLES = [
    ("setup", "⚙️ НАСТРОЙКА СЕРВЕРА"),
    ("moderation", "🛡 МОДЕРАЦИЯ"),
    ("staff", "👥 ШТАБ ГРУППЫ"),
    ("games", "🎮 ИГРЫ И КОМНАТЫ"),
    ("info", "📣 ИНФОРМАЦИЯ"),
    ("global", "🌐 ГЛОБАЛЬНАЯ АДМИНИСТРАЦИЯ"),
]

LEVEL_BADGES = {
    1: "Lv.1+",
    2: "Lv.2+",
    3: "Lv.3+",
    4: "Lv.4+",
    5: "Lv.5",
}


# ---------------------------------------------------------------- игрокам

PLAYER_COMMANDS: list[CommandMeta] = [
    # главное меню
    CommandMeta("start", "главное меню", "main", visible_to_users=True),
    CommandMeta("cmdhelp", "список игровых команд", "main", visible_to_users=True),
    CommandMeta("help", "правила игры", "main", visible_to_users=True),
    CommandMeta("cancel", "отмена текущего действия", "main", visible_to_users=True),
    # профиль и рейтинги
    CommandMeta("profile", "профиль: 🌐 глобально + 🏠 группа", "profile", visible_to_users=True),
    CommandMeta("stats", "моя статистика", "profile", visible_to_users=True),
    CommandMeta("achievements", "мои достижения", "profile", visible_to_users=True),
    CommandMeta("top", "рейтинги: 🌐 глобальный / 🏠 группы", "profile",
                visible_to_users=True,
                aliases=("top_rating", "top_wins", "top_levels")),
    CommandMeta("history", "история моих игр", "profile", visible_to_users=True),
    CommandMeta("level_info", "таблица уровней и XP", "profile", visible_to_users=True),
    CommandMeta(
        "group_stats", "статистика этой группы", "profile",
        in_private=False, visible_to_users=True,
    ),
    CommandMeta("global_stats", "глобальная статистика бота", "profile", visible_to_users=True),
    # титулы и награды
    CommandMeta("titles", "мои титулы", "badges", visible_to_users=True),
    CommandMeta("title_set", "выбрать активный титул", "badges", visible_to_users=True),
    CommandMeta("rewards", "мои ивентовые награды", "badges", visible_to_users=True),
    CommandMeta("reward_activate", "активировать награду", "badges", visible_to_users=True),
    # социальные
    CommandMeta("friends", "список друзей", "social", visible_to_users=True),
    CommandMeta("friend", "добавить друга", "social", visible_to_users=True,
                aliases=("addfriend", "fadd")),
    CommandMeta("requests", "запросы дружбы", "social", visible_to_users=True),
    CommandMeta("accept", "принять запрос дружбы", "social", visible_to_users=True),
    CommandMeta("decline", "отклонить запрос дружбы", "social", visible_to_users=True),
    CommandMeta("unfriend", "удалить из друзей", "social", visible_to_users=True,
                aliases=("fremove",)),
    CommandMeta("favorites", "избранные игроки", "social", visible_to_users=True),
    CommandMeta("favorite", "добавить в избранное", "social", visible_to_users=True,
                aliases=("fav",)),
    CommandMeta("unfavorite", "убрать из избранное", "social", visible_to_users=True,
                aliases=("unfav",)),
    CommandMeta("ignore", "добавить в игнор", "social", visible_to_users=True,
                aliases=("block",)),
    CommandMeta("unignore", "убрать из игнора", "social", visible_to_users=True,
                aliases=("unblock",)),
    CommandMeta("ignored", "список игнора", "social", visible_to_users=True),
    CommandMeta("invite", "пригласить в свою комнату", "social", visible_to_users=True),
    # игра
    CommandMeta("note", "предсмертная записка (во время партии)", "rooms", visible_to_users=True),
    CommandMeta(
        "claim", "создателю группы: забрать права админа", "rooms",
        in_private=False, visible_to_users=True,
    ),
]


# ------------------------------------------------------- администраторам

ADMIN_COMMANDS: list[CommandMeta] = [
    # настройка сервера (локально, Lv.4+; /setup также Telegram-админам)
    CommandMeta(
        "setup", "первоначальная настройка сервера", "setup",
        permission=Permission.MANAGE_SETTINGS, scope="any",
        note="также доступна Telegram-администраторам группы",
    ),
    CommandMeta(
        "settings", "настройки сервера", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_roles", "настройка состава ролей группы", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_min_players", "минимум игроков группы", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_max_players", "максимум игроков группы", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_night_time", "длительность ночи группы (сек)", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_day_time", "длительность дня группы (сек)", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_vote_time", "длительность голосования группы (сек)", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "acmdhelp", "административная справка", "setup",
        level=1, scope="any",
    ),
    CommandMeta(
        "set_game_forum", "форум партий игры этой группы", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    CommandMeta(
        "set_mafia_forum", "форум мафии этой группы", "setup",
        permission=Permission.MANAGE_SETTINGS, in_private=False,
    ),
    # модерация
    CommandMeta(
        "player", "профиль игрока (ID/@username/ответ)", "moderation",
        permission=Permission.VIEW_PROFILE, in_private=False,
        aliases=("player_stats",),
    ),
    CommandMeta(
        "players", "игроки группы", "moderation",
        permission=Permission.VIEW_PLAYERS, in_private=False,
    ),
    CommandMeta(
        "mute", "мут игрока (30m/2h/1d)", "moderation",
        permission=Permission.MUTE_PLAYER, in_private=False,
    ),
    CommandMeta(
        "unmute", "снять мут", "moderation",
        permission=Permission.MUTE_PLAYER, in_private=False,
    ),
    CommandMeta(
        "warn", "выдать варн с причиной", "moderation",
        permission=Permission.WARN_PLAYER, in_private=False,
    ),
    CommandMeta(
        "unwarn", "снять варн", "moderation",
        permission=Permission.WARN_PLAYER, in_private=False,
    ),
    CommandMeta(
        "warnings", "активные варны игрока", "moderation",
        permission=Permission.WARN_PLAYER, in_private=False,
    ),
    CommandMeta(
        "kick", "кик из группы", "moderation",
        permission=Permission.KICK_PLAYER, in_private=False,
    ),
    CommandMeta(
        "ban", "бан в группе (срок/навсегда)", "moderation",
        permission=Permission.BAN_PLAYER, in_private=False,
    ),
    CommandMeta(
        "unban", "разбан", "moderation",
        permission=Permission.BAN_PLAYER, in_private=False,
    ),
    # штаб
    CommandMeta(
        "staff", "штаб группы", "staff",
        permission=Permission.VIEW_PLAYERS, in_private=False,
    ),
    CommandMeta(
        "staff_info", "уровень администратора", "staff",
        permission=Permission.VIEW_PLAYERS, in_private=False,
    ),
    CommandMeta(
        "staff_add", "назначить админа: /staff_add ID 3", "staff",
        permission=Permission.MANAGE_STAFF, in_private=False,
        aliases=("staff_promote",),
    ),
    CommandMeta(
        "staff_remove", "снять админа", "staff",
        permission=Permission.MANAGE_STAFF, in_private=False,
        aliases=("staff_demote",),
    ),
    # игры и комнаты
    CommandMeta(
        "game", "активные игры группы", "games",
        permission=Permission.VIEW_STATS, in_private=False,
        aliases=("games", "game_info"),
    ),
    CommandMeta(
        "game_players", "состав партии", "games",
        permission=Permission.VIEW_STATS, in_private=False,
    ),
    CommandMeta(
        "game_phase", "фаза партии", "games",
        permission=Permission.VIEW_STATS, in_private=False,
    ),
    CommandMeta(
        "game_stop", "остановить игру", "games",
        permission=Permission.STOP_GAME, in_private=False,
        aliases=("game_cancel",),
    ),
    CommandMeta(
        "game_kill", "убить/оживить игрока (отладка)", "games",
        permission=Permission.MANAGE_ROOMS, in_private=False,
        note="требуется DEBUG", aliases=("game_revive",),
    ),
    CommandMeta(
        "rooms", "комнаты группы", "games",
        permission=Permission.VIEW_STATS, in_private=False,
        aliases=("room",),
    ),
    CommandMeta(
        "createroom", "комната с правилами группы", "games",
        permission=Permission.START_GAME, in_private=False,
    ),
    CommandMeta(
        "room_close", "закрыть комнату", "games",
        permission=Permission.MANAGE_ROOMS, in_private=False,
    ),
    CommandMeta(
        "room_kick", "исключить из комнаты", "games",
        permission=Permission.MANAGE_ROOMS, in_private=False,
    ),
    CommandMeta(
        "room_force_start", "принудительный старт комнаты", "games",
        permission=Permission.START_GAME, in_private=False,
        aliases=("game_start",),
    ),
    CommandMeta(
        "testgame", "тестовая игра с ботами", "games",
        permission=Permission.USE_DEBUG, scope="any",
        note="требуется DEBUG MODE / debug группы",
    ),
    CommandMeta(
        "debug", "debug-инструменты партий", "games",
        permission=Permission.USE_DEBUG, scope="any",
        note="требуется DEBUG MODE / debug группы",
        aliases=("debug_game", "debug_state", "debug_phase", "debug_finish_phase"),
    ),
    # информация
    CommandMeta(
        "botstats", "статистика бота", "info",
        permission=Permission.VIEW_STATS, scope="any",
    ),
    CommandMeta(
        "broadcast", "объявление в группу", "info",
        permission=Permission.BROADCAST, in_private=False,
        aliases=("announce",),
    ),
    CommandMeta(
        "logs", "последние действия (аудит)", "info",
        permission=Permission.BROADCAST, in_private=False,
    ),
    # глобальная администрация (только .env: OWNER_IDS / ADMIN_IDS)
    CommandMeta(
        "admin", "админ-панель", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "reload", "перезагрузить конфигурацию", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "reward_create", "создать ивентовую награду", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "reward_grant", "выдать награду игроку", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "reward_list", "список наград", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "title_grant", "выдать титул", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "title_list", "список титулов", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "title_remove", "снять титул", "global",
        level=4, scope="global",
    ),
    CommandMeta(
        "maintenance", "режим обслуживания", "global",
        permission=Permission.MANAGE_GLOBAL_SETTINGS, scope="any",
    ),
    CommandMeta(
        "owner", "Owner-панель", "global",
        level=5, scope="global",
    ),
    CommandMeta(
        "debug_help", "диагностика прав и команд", "global",
        level=5, scope="global",
    ),
    CommandMeta(
        "achievement_grant", "выдать достижение", "global",
        level=5, scope="global",
    ),
    CommandMeta(
        "achievement_remove", "снять достижение", "global",
        level=5, scope="global",
    ),
    CommandMeta(
        "set_rating", "изменить рейтинг игрока",
        "global", level=5, scope="global",
        aliases=("add_rating", "set_wins", "add_wins", "set_xp", "add_xp",
                 "set_level"),
    ),
]


# ---------------------------------------------------------------- утилиты


def min_level(meta: CommandMeta) -> int:
    """Минимальный уровень команды: из существующих LEVEL_PERMISSIONS.

    Порог вычисляется из реальной таблицы прав — справка не может
    «придумать» уровень, отличный от проверок хендлеров.
    """
    if meta.permission is not None:
        levels = [
            lvl for lvl, perms in LEVEL_PERMISSIONS.items()
            if meta.permission in perms
        ]
        if levels:
            return int(min(levels))
    return int(meta.level)


def _command_lines(metas: list[CommandMeta], *, with_level: bool) -> list[str]:
    lines: list[str] = []
    for meta in metas:
        line = f"/{meta.command} — {meta.description}"
        if with_level:
            lvl = min_level(meta)
            if lvl > 0:
                line += f" <b>[{LEVEL_BADGES.get(lvl, f'Lv.{lvl}+')}]</b>"
        lines.append(line)
        if meta.note:
            lines.append(f"    <i>· {meta.note}</i>")
    return lines


def player_help_text(*, in_group: bool) -> str:
    """Текст /cmdhelp: только команды обычных игроков, по категориям."""
    lines = ["🎮 <b>КОМАНДЫ MAFIA ONLINE</b>", ""]
    for key, title in PLAYER_CATEGORY_TITLES:
        items = [
            m for m in PLAYER_COMMANDS
            if m.category == key and (in_group or m.in_private)
        ]
        if not items:
            continue
        lines.append(title)
        lines += _command_lines(items, with_level=False)
        lines.append("")
    lines.append("⚙️ Административная справка: /acmdhelp (для админов)")
    return "\n".join(lines)


def admin_help_text(*, level: int, is_global: bool, in_group: bool) -> str:
    """Текст /acmdhelp — команды строго по фактическому уровню пользователя.

    level — эффективный уровень (локальный в группе / глобальный в ЛС);
    is_global — выдан глобально (.env), глобальные команды показываются
    только таким пользователям.
    """
    from bot.services.permissions import LEVEL_TITLES

    title = LEVEL_TITLES.get(AdminLevel(level), "👤 Player")
    lines = [
        "⚙️ <b>АДМИНИСТРАТИВНЫЕ КОМАНДЫ</b>", "",
        f"Ваш уровень: <b>{title}</b>"
        + (" · 🌐 глобальный" if is_global else " · 🏠 этой группы"), "",
    ]
    shown = 0
    for key, cat_title in ADMIN_CATEGORY_TITLES:
        items = []
        for meta in ADMIN_COMMANDS:
            if meta.category != key:
                continue
            if min_level(meta) > level:
                continue                       # уровень ниже требуемого — скрыто
            if meta.scope == "global" and not is_global:
                continue                       # глобальные — только глобальной админам
            if not in_group and not meta.in_private:
                continue                       # групповые команды в ЛС скрыты
            items.append(meta)
        if not items:
            continue
        shown += len(items)
        lines.append(cat_title)
        lines += _command_lines(items, with_level=True)
        lines.append("")
    if shown == 0:
        lines.append("Доступных административных команд нет.")
    lines.append(
        "<i>Справка носит информационный характер: каждая команда "
        "самостоятельно проверяет права при вызове.</i>"
    )
    return "\n".join(lines)


__all__ = [
    "ADMIN_COMMANDS",
    "CommandMeta",
    "LEVEL_BADGES",
    "PLAYER_COMMANDS",
    "admin_help_text",
    "min_level",
    "player_help_text",
]
