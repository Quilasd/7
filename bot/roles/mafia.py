"""🔴 Мафия."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="mafia",
        name="МАФИЯ",
        emoji="🔴",
        team=Team.MAFIA,
        description=(
            "Твоя задача — уничтожить мирных жителей. Ночью вы вместе с "
            "союзниками выбираете жертву, днём притворяйтесь мирными."
        ),
        night_action=ActionType.KILL,
        action_prompt="Кого устранить этой ночью?",
        action_verb="Устранить",
        knows_teammates=True,
        win_condition_text="Мафия побеждает, когда мафии не меньше, чем всех остальных живых.",
    )
)
