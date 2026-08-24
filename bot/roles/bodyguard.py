"""🛡 Телохранитель."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="bodyguard",
        name="ТЕЛОХРАНИТЕЛЬ",
        emoji="🛡",
        team=Team.CITY,
        description=(
            "Каждую ночь ты защищаешь одного игрока (кроме себя). Если ночью "
            "в него выстрелят — он выживет, но ты погибнешь вместо него."
        ),
        night_action=ActionType.PROTECT,
        action_prompt="Кого защищать этой ночью?",
        action_verb="Защищать",
        can_target_self=False,
        sacrifice_on_save=True,
        win_condition_text="Мирные побеждают, когда вся мафия уничтожена.",
    )
)
