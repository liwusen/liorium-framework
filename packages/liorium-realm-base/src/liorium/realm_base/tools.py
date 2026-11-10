"""``@realm_tool``：把 realm 的方法标记成工具，交给 ``BaseRealm.tools()`` 自动收集。

**本模块 import 期不依赖 langchain**：装饰器只往函数上打标记，真正构建
``StructuredTool`` 发生在 :func:`build_realm_tools`（且只在确有标记方法时才 import
``langchain_core.tools``）。这样"只提供 VFS 挂载 + 周期上下文的 realm"不必安装
langchain；langchain-core 是基础依赖，跟着 ``pip install liorium`` 一起装好。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from liorium.core.errors import RealmError

F = TypeVar("F", bound=Callable[..., Any])

_MARK = "__liorium_realm_tool__"
"""标记属性名；值 = 传给 ``langchain_core.tools.tool`` 的关键字参数。"""


def realm_tool(
    fn: F | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    **tool_kwargs: Any,
) -> F | Callable[[F], F]:
    """把一个 realm 方法声明成工具（轻量标记，不构建对象）。

    用法::

        class TodoRealm(BaseRealm):
            @realm_tool
            async def todo_add(self, text: str) -> str:
                \"\"\"加一条待办。\"\"\"

    - 工具名默认取方法名；``description`` 默认取 docstring；
    - 其余关键字（``return_direct`` / ``args_schema`` …）原样转给
      ``langchain_core.tools.tool``；
    - 只标记、不注册：``tools()`` 的默认实现会按**类定义顺序**收集（子类覆盖父类同名
      方法时只保留子类那份）。自己重写了 ``tools()`` 的 realm 不受影响。
    """

    def mark(func: F) -> F:
        setattr(
            func,
            _MARK,
            {"name": name, "description": description, **tool_kwargs},
        )
        return func

    if fn is not None:
        return mark(fn)
    return mark


def is_realm_tool(value: Any) -> bool:
    return callable(value) and getattr(value, _MARK, None) is not None


def marked_methods(cls: type) -> list[tuple[str, Any]]:
    """``[(方法名, 未绑定函数)]``，按类定义顺序；子类覆盖父类同名方法只留一份。"""
    ordered: dict[str, Any] = {}
    for base in reversed(cls.__mro__):
        for attr, value in vars(base).items():
            if is_realm_tool(value):
                ordered[attr] = value
    return list(ordered.items())


def build_realm_tools(instance: Any) -> list[Any]:
    """把 ``instance`` 上所有 ``@realm_tool`` 方法包成 LangChain 工具。

    没有标记方法时返回 ``[]`` 且**不 import langchain**（零成本）。
    """
    methods = marked_methods(type(instance))
    if not methods:
        return []
    try:
        from langchain_core.tools import tool as _tool
    except ImportError as exc:  # pragma: no cover - 只在没装 langchain 时触发
        raise RealmError(
            "realm 用了 @realm_tool，但没有安装 langchain-core："
            "pip install 'liorium'（langchain-core 是基础依赖）"
        ) from exc

    built: list[Any] = []
    for attr, func in methods:
        spec = dict(getattr(func, _MARK) or {})
        tool_name = spec.pop("name", None) or attr
        description = spec.pop("description", None) or (func.__doc__ or "").strip()
        built.append(
            _tool(tool_name, description=description or None, **spec)(getattr(instance, attr))
        )
    return built


__all__ = ["build_realm_tools", "is_realm_tool", "marked_methods", "realm_tool"]
