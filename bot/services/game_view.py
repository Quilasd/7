"""Построение текстов игровых сообщений.

Все тексты собираются здесь, чтобы фазы/хендлеры оставались тонкими.
Пользовательский ввод всегда экранируется (esc).
"""

from __future__ import annotations

from bot.database.models import Game, GamePlayer, GameStatus, PlayerStatus, Room, RoomStatus, User
from bot.roles import get_role, team_of
from bot.services.night_resolver import NightOutcome
from bot.services.vote_manager import VoteResolution
from bot.utils.helpers import deadline_in, display_name, esc, fmt_mmss

DEATH_CAUSE_EMOJI = {
    "mafia": "🔴",      # убийство мафии
    "maniac": "🔪",     # маньяк
    "vote": "🗳",       # изгнан голосованием
    "left": "🚪",       # покинул игру
    "sacrifice": "🛡",  # телохранитель
}


def room_status_emoji(rp, room: Room) -> str:
    """🟢 готов / 🟡 не готов / 💀 мёртв / ⚪ наблюдатель."""
    if room.status in (RoomStatus.PLAYING.value, RoomStatus.FINISHED.value) and room.game_id:
        return "—"  # в игре статус виден в игровой сводке
    return "🟢" if rp.is_ready else "🟡"


def room_text(room: Room, players: list, game: Game | None = None) -> str:
    lines = [
        f"🎭 <b>МАФИЯ #{room.id}</b>",
        f"<i>{esc(room.name)}</i>",
        "",
        f"👥 Игроки: <b>{len(players)}/{room.max_players}</b>",
        f"🔐 Приватная" if room.is_private else "🌍 Публичная",
        "",
    ]
    for index, rp in enumerate(players, start=1):
        user = rp.user
        mark = "👑" if user.id == room.creator_id else room_status_emoji(rp, room)
        ready = " 🟢" if rp.is_ready else ""
        lines.append(f"{index}. {mark} {esc(display_name(user))}{ready}")
    lines.append("")
    if room.status == RoomStatus.OPEN.value:
        lines.append("⏳ Ожидание игроков. Нажмите «Готов», когда все собрались.")
        lines.append(f"Старт возможен от {room.min_players} игроков (все должны быть готовы).")
    elif room.status == RoomStatus.PLAYING.value and game:
        lines.append(f"🎮 Идёт игра — фаза: <b>{game.status}</b>, день {game.day_number}.")
    elif room.status == RoomStatus.CLOSED.value:
        lines.append("❌ Комната закрыта.")
    elif room.status == RoomStatus.FINISHED.value:
        lines.append("🏁 Игра завершена.")
    return "\n".join(lines)


def roles_setup_text(setup: dict[str, int], max_players: int) -> str:
    lines = ["🎭 <b>НАБОР РОЛЕЙ</b>", ""]
    total = 0
    for role_id, count in setup.items():
        role = get_role(role_id)
        if not role:
            continue
        total += count
        lines.append(f"{role.emoji} {role.name}: <b>{count}</b>")
    citizens = max(0, max_players - total)
    lines.append(f"🔵 МИРНЫЕ (автоматически): все остальные ({citizens}+ слотов)")
    lines.append("")
    lines.append(f"👥 Максимум игроков: <b>{max_players}</b>, занято ролями: <b>{total}</b>")
    return "\n".join(lines)


def role_card(game_player: GamePlayer, teammates: list[User] | None = None) -> str:
    role = get_role(game_player.role)
    lines = [
        "🎭 <b>ТВОЯ РОЛЬ</b>",
        "",
        f"<b>{role.title}</b>" if role else "❓ РОЛЬ НЕ ИЗВЕСТНА",
        "",
        role.description if role else "",
    ]
    if role and role.win_condition_text:
        lines += ["", f"🏆 {role.win_condition_text}"]
    if teammates:
        lines += ["", "Твои союзники:", "• " + "\n• ".join(esc(display_name(u)) for u in teammates)]
    if role and role.night_action:
        lines += ["", f"🌙 Ночью: {role.action_prompt}"]
    return "\n".join(lines)


def night_header(game: Game) -> str:
    return f"🌙 <b>НОЧЬ #{game.day_number}</b>\n\nГород засыпает..."


def day_header(game: Game) -> str:
    return f"☀️ <b>ДЕНЬ #{game.day_number}</b>"


def alive_list(players: list[GamePlayer]) -> str:
    names = [esc(display_name(p.user)) for p in players if p.is_alive]
    return "\n".join(f"• {name}" for name in names)


def morning_text(game: Game, outcome: NightOutcome, players: list[GamePlayer], reveal_roles: bool) -> str:
    lines = ["☀️ <b>НАСТУПИЛО УТРО</b>", ""]
    if not outcome.deaths:
        lines.append("✨ Этой ночью никто не погиб.")
    else:
        by_id = {p.user_id: p for p in players}
        for death in outcome.deaths:
            victim = by_id.get(death.user_id)
            if victim is None:
                continue
            name = esc(display_name(victim.user))
            role_part = ""
            if reveal_roles and victim.role:
                role = get_role(victim.role)
                role_part = f" ({role.title})" if role else ""
            lines.append(f"💀 Этой ночью погиб <b>{name}</b>{role_part}.")
    lines += ["", f"👥 В живых: {sum(1 for p in players if p.is_alive)}", alive_list(players)]
    return "\n".join(lines)


def death_personal_text(game_player: GamePlayer, cause: str) -> str:
    role = get_role(game_player.role)
    emoji = DEATH_CAUSE_EMOJI.get(cause, "💀")
    reason = {
        "mafia": "Тебя убили ночью.",
        "maniac": "Тебя убил маньяк.",
        "vote": "Город изгнал тебя голосованием.",
        "left": "Ты покинул игру.",
        "sacrifice": "Ты погиб, спасая игрока, которому доверял.",
    }.get(cause, "Ты выбыл из игры.")
    return (
        f"{emoji} <b>ТЫ ВЫБЫЛ ИЗ ИГРЫ</b>\n\n{reason}\n\n"
        f"Твоя роль: {role.title if role else '—'}.\n"
        "Ты остаёшься наблюдателем: роль и события дня видны, но влиять на игру нельзя."
    )


def death_note_text(victim: GamePlayer | None, text: str | None) -> str:
    """Утренняя публикация предсмертной записки (или нейтральное сообщение)."""
    name = esc(display_name(victim.user)) if victim else "Игрок"
    if not text:
        return f"☠️ <b>{name}</b> ничего не успел сказать..."
    return f"📝 Последние слова <b>{name}</b>:\n\n«{esc(text)}»"


def day_text(game: Game, players: list[GamePlayer], seconds: int) -> str:
    return "\n".join(
        [
            f"{day_header(game)}",
            "",
            "💬 Обсуждение. Обсудите подозрения — скоро голосование.",
            f"⏱ До голосования: <b>{fmt_mmss(seconds)}</b>",
            "",
            "👥 Живые игроки:",
            alive_list(players),
        ]
    )


def voting_text(game: Game, candidates: list[GamePlayer], seconds: int, round_no: int) -> str:
    header = f"🗳 <b>ГОЛОСОВАНИЕ</b>" + (f" · круг {round_no}" if round_no > 1 else "")
    lines = [header, "", "Кого изгнать из города?", f"⏱ Время: <b>{fmt_mmss(seconds)}</b>", ""]
    lines += [f"• {esc(display_name(p.user))}" for p in candidates]
    return "\n".join(lines)


def vote_results_text(game: Game, resolution: VoteResolution, players: list[GamePlayer]) -> str:
    by_id = {p.user_id: p for p in players}
    lines = ["📊 <b>РЕЗУЛЬТАТЫ ГОЛОСОВАНИЯ</b>", ""]
    if not resolution.votes:
        lines.append("Никто не проголосовал.")
        return "\n".join(lines)
    for target_id, count in resolution.votes:
        target = by_id.get(target_id)
        name = esc(display_name(target.user)) if target else f"#{target_id}"
        lines.append(f"{name} — {count} голос{'а' if count in (2, 3, 4) or count % 10 in (2,3,4) and count < 20 else 'ов'}")
    lines.append("")
    if resolution.lynched:
        victim = by_id.get(resolution.lynched)
        if victim:
            name = esc(display_name(victim.user))
            role = get_role(victim.role)
            lines.append(f"💀 <b>{name} исключён из игры.</b>")
            if game.get_setting("reveal_roles_on_death", True) and role:
                lines.append(f"Его роль: {role.title}")
    else:
        lines.append("⚖️ Ничья — никто не покидает город.")
    return "\n".join(lines)


def game_over_text(
    game: Game,
    title: str,
    players: list[GamePlayer],
    winner_user_ids: set[int],
    reason: str | None = None,
) -> str:
    from bot.roles import Team

    winner_players = [p for p in players if p.user_id in winner_user_ids]
    mafia = [p for p in players if team_of(p.role) == Team.MAFIA and p.status != PlayerStatus.SPECTATOR.value]
    lines = [
        "🏆 <b>ИГРА ОКОНЧЕНА</b>",
        "",
        f"<b>{title}</b>",
        "",
    ]
    if winner_players:
        lines.append("👑 Победители:")
        lines += [f"• {esc(display_name(p.user))}" for p in winner_players]
        lines.append("")
    if mafia:
        lines.append("🔴 Мафия была:")
        lines += [
            f"• {esc(display_name(p.user))} — {get_role(p.role).title if get_role(p.role) else ''}"
            for p in mafia
        ]
        lines.append("")
    lines.append("💡 Полный расклад ролей:")
    lines += [
        f"• {esc(display_name(p.user))} — {get_role(p.role).title if get_role(p.role) else '—'}"
        + ("" if p.is_alive else " 💀")
        for p in players
        if p.status != PlayerStatus.SPECTATOR.value
    ]
    if reason:
        lines.append("")
        lines.append(f"ℹ️ {esc(reason)}")
    return "\n".join(lines)


def _side_team(game: Game):
    from bot.roles import Team

    side = game.winner
    if side == "mafia":
        return Team.MAFIA
    if side == "maniac":
        return Team.NEUTRAL
    return Team.CITY


def personal_result_text(won: bool, is_draw: bool, rating_delta: int, xp_delta: int) -> str:
    if is_draw:
        return "🤝 Ничья. Рейтинг не изменился."
    if won:
        return f"🎉 Ты в числе победителей! Рейтинг: +{rating_delta}, опыт: +{xp_delta}."
    return f"😔 Поражение. Рейтинг: {rating_delta}, опыт: +{xp_delta}."


def game_status_text(game: Game, game_player: GamePlayer, players: list[GamePlayer]) -> str:
    role = get_role(game_player.role)
    phase_names = {
        GameStatus.STARTING.value: "⏳ Подготовка",
        GameStatus.NIGHT.value: "🌙 Ночь",
        GameStatus.DAY.value: "☀️ День (обсуждение)",
        GameStatus.VOTING.value: "🗳 Голосование",
        GameStatus.ENDED.value: "🏁 Завершена",
    }
    phase = phase_names.get(game.status, game.status)
    lines = [
        "🎮 <b>СОСТОЯНИЕ ИГРЫ</b>",
        "",
        f"Фаза: <b>{phase}</b>",
        f"День: <b>{game.day_number}</b>",
        f"👥 В живых: <b>{sum(1 for p in players if p.is_alive)}</b> из {len(players)}",
    ]
    if game.phase_deadline and game.status != GameStatus.ENDED.value:
        lines.append(f"⏱ До конца фазы: {fmt_mmss(deadline_in(game.phase_deadline))}")
    if game_player.is_alive:
        lines += ["", f"Твоя роль: {role.title if role else '—'}", "👥 Живые игроки:", alive_list(players)]
    else:
        lines += ["", "💀 Ты выбыл — наблюдаешь за игрой."]
    return "\n".join(lines)
