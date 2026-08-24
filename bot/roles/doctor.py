"""❤️ Доктор."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="doctor",
        name="ДОКТОР",
        emoji="❤️",
        team=Team.CITY,
        description=(
            "Каждую ночь ты выбираешь игрока для лечения. Если его попытаются "
            "убить — он выживет. Нельзя лечить одного и того же две ночи подряд."
        ),
        night_action=ActionType.HEAL,
        action_prompt="Кого лечить этой ночью?",
        action_verb="Вылечить",
        can_target_self=True,
        no_repeat_target=True,
        win_condition_text="Мирные побеждают, когда вся мафия уничтожена.",
    )
)
