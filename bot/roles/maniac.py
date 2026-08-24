"""🔪 Маньяк — независимая роль."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="maniac",
        name="МАНЬЯК",
        emoji="🔪",
        team=Team.NEUTRAL,
        description=(
            "Ты играешь сам за себя. Каждую ночь выбираешь жертву. "
            "Тебе всё равно, кто победит в войне мафии и города — главное, "
            "остаться последним."
        ),
        night_action=ActionType.KILL,
        action_prompt="Кого убить этой ночью?",
        action_verb="Убить",
        win_condition_text=(
            "Маньяк побеждает, если остался один или среди двух последних игроков."
        ),
    )
)
