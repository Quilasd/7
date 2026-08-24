"""🔵 Мирный житель."""

from bot.roles.base import Role, Team, register_role

register_role(
    Role(
        id="citizen",
        name="МИРНЫЙ ЖИТЕЛЬ",
        emoji="🔵",
        team=Team.CITY,
        description=(
            "У тебя нет ночных способностей, но есть голос и здравый смысл. "
            "Днём обсуждай подозрительных и голосуй — от этого зависит город."
        ),
        win_condition_text="Мирные побеждают, когда вся мафия уничтожена.",
    )
)
