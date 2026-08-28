"""Инвариант полноты Command Registry (двусторонний).

REAL → REGISTRY: каждая Telegram-команда, зарегистрированная в роутерах
через Command(...)/CommandStart(), описана в Command Registry (основным
именем или алиасом) — иначе разработчик забыл добавить её в справку.

REGISTRY → REAL: в Registry нет «призрачных» команд — каждая описанная
команда/алиас реально зарегистрирована в handlers (защита от удаления
команды из проекта без чистки справки).

Источник «реальных» команд — САМО ДЕРЕВО РОУТЕРОВ aiogram
(get_root_router() → sub_routers → message.handlers → Command-фильтры):
никакого поиска строк по исходникам, комментариев и текстов сообщений.

Алиасы: один handler с Command("a", "b") — обе команды реальные; registry
описывает их компактно (CommandMeta.command + CommandMeta.aliases).

Нормализация: имя без «/», в нижнем регистре (aiogram Command с
ignore_case=True сам приводит команды к lower — сравнение согласовано).

Механизм исключений (REGISTRY_EXCLUDED_COMMANDS) не используется: на
момент написания все зарегистрированные команды покрыты реестром.
Если появится намеренно скрытая из справки команда — вернись сюда и
добавь явное исключение, а не расширяй набор тестов.
"""

from __future__ import annotations

from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from aiogram import Router

from bot.utils.command_registry import ADMIN_COMMANDS, PLAYER_COMMANDS

ALL_META = PLAYER_COMMANDS + ADMIN_COMMANDS

# модули-роутеры хендлеров (фактические глобальные объекты aiogram);
# get_root_router() намеренно НЕ вызывается: повторная сборка дерева
# невозможна (роутеры прикрепляются один раз) и ломала бы другие тесты
_HANDLER_MODULES = (
    "admin", "cmdhelp", "game", "game_chats", "groups_admin", "history",
    "owner", "profile", "ratings", "rewards", "rooms", "setup", "social",
    "start", "testgame", "voting",
)


def _handler_routers() -> list[Router]:
    import importlib

    routers = []
    for name in _HANDLER_MODULES:
        module = importlib.import_module(f"bot.handlers.{name}")
        routers.append(module.router)
    return routers


# ------------------------------------------------------------- сбор команд


def normalize(name: str) -> str:
    """'/Test' и 'test' — одна команда (aiogram ignore_case, без слэша)."""
    return name.strip().lstrip("/").lower()


def _roots_of(router: Router) -> Router:
    """Верхний узел дерева, в котором живёт роутер (root/Dispatcher)."""
    node = router
    while getattr(node, "_parent_router", None) is not None:
        node = node._parent_router
    return node


def collect_registered_commands(router: Router | None = None) -> set[str]:
    """Все реальные Telegram-команды из фактических роутеров проекта.

    Обходит message-observer каждого роутера и собирает имена из
    Command-фильтров (CommandStart — подкласс Command, /start включён).
    FSM-хендлеры и callback-роутеры не создают команд — не учитываются;
    динамические regexp-команды (/game_<ID>) командами не являются.

    Точки входа — глобальные роутеры модулей bot/handlers/* (подъём к их
    корню, если дерево уже собрано Dispatcher-тестами). Это делает сбор
    независимым от порядка прогонов и не требует повторной сборки root.
    """
    if router is not None:
        roots = [router]
    else:
        roots, seen = [], set()
        for handler_router in _handler_routers():
            root = _roots_of(handler_router)
            if id(root) not in seen:
                seen.add(id(root))
                roots.append(root)
    commands: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        observer = getattr(current, "message", None)
        if observer is not None:
            for handler in observer.handlers:
                for wrapped in (handler.filters or []):
                    inner = getattr(wrapped, "callback", wrapped)
                    if isinstance(inner, Command):
                        commands.update(normalize(c) for c in inner.commands)
        stack.extend(getattr(current, "sub_routers", []))
    return commands


def registry_names(metas=ALL_META) -> set[str]:
    """Все имена из реестра: основные команды + алиасы."""
    names: set[str] = set()
    for meta in metas:
        names.add(normalize(meta.command))
        names.update(normalize(a) for a in meta.aliases)
    return names


# ------------------------------------------------------------- инварианты


class TestRegistryCompleteness:
    """Двусторонняя сверка реестра с реальными командами роутеров."""

    def test_all_registered_commands_are_in_registry(self):
        """REAL → REGISTRY: забытая в справке команда роняет тест
        с явным списком отсутствующих."""
        registered = collect_registered_commands()
        missing = sorted(registered - registry_names())
        assert not missing, (
            "❌ Command Registry incomplete\n\n"
            "Registered commands missing from registry:\n\n"
            + "\n".join(f"/{cmd}" for cmd in missing)
            + "\n\nPlease add them to `bot/utils/command_registry.py` "
              "(CommandMeta или aliases существующей команды)."
        )

    def test_registry_has_no_unknown_commands(self):
        """REGISTRY → REAL: призрачная команда (удалена из handlers) роняет тест."""
        registered = collect_registered_commands()
        unknown = sorted(registry_names() - registered)
        assert not unknown, (
            "❌ Command Registry contains unknown commands:\n\n"
            + "\n".join(f"/{cmd}" for cmd in unknown)
            + "\n\nThese commands are NOT registered in any router — remove "
              "them from `bot/utils/command_registry.py`."
        )

    def test_no_duplicate_commands_in_registry(self):
        """Одна команда не должна иметь двух конфликтующих записей."""
        seen: dict[str, str] = {}
        duplicates: set[str] = set()
        for meta in ALL_META:
            for name in (meta.command, *meta.aliases):
                key = normalize(name)
                if key in seen:
                    duplicates.add(key)
                seen[key] = meta.command
        assert not duplicates, (
            "❌ Duplicate command metadata:\n\n"
            + "\n".join(f"/{cmd}" for cmd in sorted(duplicates))
        )

    def test_registry_covers_every_router_command(self):
        """Сборщик видит команды КАЖДОГО роутера хендлеров; если дерево
        уже собрано (Dispatcher/root), все роутеры лежат в ОДНОМ дереве
        — «тихо потерянный» include_router был бы замечен."""
        routers = _handler_routers()
        assert len(routers) >= 10
        attached = [r for r in routers
                    if getattr(r, "_parent_router", None) is not None]
        if attached:  # дерево собрано (порядок прогонов не важен)
            roots = {_roots_of(r) for r in attached}
            assert len(roots) == 1, "роутеры хендлеров расщеплены по деревьям"
        # sanity: ключевые команды разных роутеров находятся
        registered = collect_registered_commands()
        for cmd in ("start", "setup", "settings", "acmdhelp", "owner",
                    "testgame", "set_game_forum", "claim", "history"):
            assert cmd in registered, cmd


class TestMechanism:
    """Проверка самого механизма (§14): тестовая команда без записи в
    реестре обязана обнаруживаться — и пропадать после добавления."""

    def _fake_router_with(self, *commands: str) -> Router:
        """Изолированный роутер с «только что добавленными» командами."""
        router = Router(name="synthetic")

        @router.message(Command(*commands))
        async def _handler(message: Message) -> None:  # pragma: no cover
            return None

        return router

    def test_new_command_without_registry_is_detected(self):
        """Разработчик добавил команду, но забыл реестр → разница найдена."""
        router = self._fake_router_with("brand_new_test_command")
        registered = collect_registered_commands(router)
        missing = sorted(registered - registry_names())
        assert missing == ["brand_new_test_command"]

    def test_command_passes_after_added_to_registry(self):
        """Команда добавлена в реестр (как CommandMeta/alias) → инвариант зелёный."""
        from bot.utils.command_registry import CommandMeta

        router = self._fake_router_with("brand_new_test_command")
        metas = ALL_META + [
            CommandMeta("brand_new_test_command", "тест", "main",
                        visible_to_users=True)
        ]
        registered_synth = collect_registered_commands(router)
        names = registry_names(metas)
        # REAL → REGISTRY: всё покрыто (в т.ч. новая команда)
        assert not (registered_synth - names)
        # REGISTRY → REAL: из полного набора «неизвестных» нет
        real_all = collect_registered_commands() | {"brand_new_test_command"}
        assert not (names - real_all)

    def test_removed_command_detected_as_unknown(self):
        """Старую команду удалили из handlers, но забыли в справке → unknown."""
        from bot.utils.command_registry import CommandMeta

        metas = ALL_META + [
            CommandMeta("deleted_command", "устаревшая", "main",
                        visible_to_users=True)
        ]
        unknown = sorted(registry_names(metas) - collect_registered_commands())
        assert unknown == ["deleted_command"]

    def test_duplicate_aliases_detected(self):
        from bot.utils.command_registry import CommandMeta

        metas = ALL_META + [
            CommandMeta("profile", "дубль профиля", "main",
                        visible_to_users=True)
        ]
        seen: dict[str, str] = {}
        duplicates: set[str] = set()
        for meta in metas:
            for name in (meta.command, *meta.aliases):
                key = normalize(name)
                if key in seen:
                    duplicates.add(key)
                seen[key] = meta.command
        assert "profile" in duplicates

    def test_aliases_are_real_commands(self):
        """Каждый алиас реестра — реально зарегистрированная команда
        (alias не может «повиснуть» после рефакторинга хендлера)."""
        registered = collect_registered_commands()
        for meta in ALL_META:
            for alias in meta.aliases:
                assert normalize(alias) in registered, (
                    f"alias /{alias} команды /{meta.command} не зарегистрирован"
                )


class TestNormalization:
    """§6: нормализация имён — регистр и слэш не создают фантомных различий."""

    def test_normalize_slash_and_case(self):
        assert normalize("/TEST") == normalize("test")
        assert normalize("Test") == "test"
        assert normalize("/cmdhelp") == "cmdhelp"

    def test_aiogram_command_filters_are_lowercased(self):
        """aiogram Command(ignore_case=True) хранит lower — сборщик и реестр
        согласованы по формату (никаких '/Test' против 'test')."""
        router = self._router()

        @router.message(Command("SomeCase", "AnotherCase"))
        async def _h(message: Message) -> None:  # pragma: no cover
            return None

        collected = collect_registered_commands(router)
        assert {"somecase", "anothercase"} <= collected

    @staticmethod
    def _router() -> Router:
        return Router(name="norm")

    def test_command_start_included(self):
        """/start (CommandStart) собирается как обычная команда."""
        router = self._router()

        @router.message(CommandStart())
        async def _start(message: Message) -> None:  # pragma: no cover
            return None

        assert "start" in collect_registered_commands(router)
