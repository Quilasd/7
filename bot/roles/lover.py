"""👰 Любовница."""

from bot.roles.base import ActionType, Role, Team, register_role

register_role(
    Role(
        id="lover",
        name="ЛЮБОВНИЦА",
        emoji="👰",
        team=Team.CITY,
        description=(
            "Каждую ночь ты проводишь время с одним из игроков и блокируешь "
            "его ночное действие: мафия не убьёт, доктор не вылечит, "
            "комиссар не проверит. Игрок не узнает, что был заблокирован."
        ),
        night_action=ActionType.BLOCK,
        action_prompt="С кем провести эту ночь? (блокировка действия)",
        action_verb="Заблокировать",
        win_condition_text="Мирные побеждают, когда вся мафия уничтожена.",
    )
)
