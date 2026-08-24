"""🕵️ Комиссар."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="detective",
        name="КОМИССАР",
        emoji="🕵️",
        team=Team.CITY,
        description=(
            "Каждую ночь ты можешь проверить одного игрока и узнать, "
            "является ли он мафией. Результат приходит утром в личку."
        ),
        night_action=ActionType.CHECK,
        action_prompt="Кого проверить этой ночью?",
        action_verb="Проверить",
        win_condition_text="Мирные побеждают, когда вся мафия уничтожена.",
    )
)
