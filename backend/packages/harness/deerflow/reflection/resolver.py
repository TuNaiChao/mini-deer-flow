"""
动态解析模块

通过 'module.path:ClassName' 格式的字符串动态加载类，
避免硬编码 import，实现完全配置驱动。

错误信息设计（Phase 0 重审对齐上游）：缺包 / 路径畸形 / 属性不存在三种失败
全部归一成 :class:`ImportError` 并附**可操作的安装提示**——这是新手最常踩的坑
（忘了 ``uv add langchain-google-genai``、把 ``module:Attr`` 写成 ``module.Attr``），
提示里直接给出 ``uv add`` 命令。
"""

import importlib
from typing import TypeVar

T = TypeVar("T")

# 模块名 → pip 包名的提示映射，用于提供友好的安装提示。
# 「模块名」（import 路径的根，下划线）与「包名」（pip 安装名，连字符）常不同。
MODULE_TO_PACKAGE_HINTS = {
    "langchain_openai": "langchain-openai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_deepseek": "langchain-deepseek",
    "langchain_ollama": "langchain-ollama",
    "langchain_google_genai": "langchain-google-genai",
}


def _build_missing_dependency_hint(module_path: str, err: ImportError) -> str:
    """模块 import 失败时构建可操作的安装提示。

    优先用 :class:`ModuleNotFoundError` 的 ``name`` 属性定位**真正缺失的模块**
    （可能是间接依赖，例如 ``langchain_google_genai`` 触发的 ``google``），
    再查 ``MODULE_TO_PACKAGE_HINTS`` 映射出 pip 包名；查不到就把模块名 ``_`` → ``-``
    当作包名兜底。
    """
    module_root = module_path.split(".", 1)[0]
    missing_module = getattr(err, "name", None) or module_root

    # 已知集成的 provider 即使是间接依赖（如 ``google``）也优先给 provider 包提示。
    package_name = MODULE_TO_PACKAGE_HINTS.get(module_root)
    if package_name is None:
        package_name = MODULE_TO_PACKAGE_HINTS.get(missing_module, missing_module.replace("_", "-"))

    return f"缺少依赖 '{missing_module}'。请运行: uv add {package_name}（或 `pip install {package_name}`），然后重启 DeerFlow。"


def resolve_variable(
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """从 'module.path:variable_name' 字符串动态加载变量/类。

    Args:
        variable_path: 模块路径和变量名，用 ``:`` 分隔。
        expected_type: 期望的类型（单个或元组），用 :func:`isinstance` 校验。
            加载**实例**用这个；加载**类**请用 :func:`resolve_class`。

    Returns:
        解析后的变量/类。

    Raises:
        ImportError: 路径畸形 / 模块缺失或 import 出错 / 属性不存在。
        ValueError: ``expected_type`` 校验不通过。
    """
    try:
        module_path, var_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        # 没有 ``:`` → rsplit 只返回 1 段，解包失败。归一成 ImportError 带示例。
        raise ImportError(f"{variable_path} 看起来不像变量路径。示例: parent_package.sub_package.module_name:variable_name") from err

    try:
        module = importlib.import_module(module_path)
    except ImportError as err:
        module_root = module_path.split(".", 1)[0]
        err_name = getattr(err, "name", None)
        # 仅「模块缺失」给安装提示；其它 ImportError（模块内部错）保留原信息。
        if isinstance(err, ModuleNotFoundError) or err_name == module_root:
            hint = _build_missing_dependency_hint(module_path, err)
            raise ImportError(f"无法 import 模块 {module_path}。{hint}") from err
        raise ImportError(f"import 模块 {module_path} 时出错: {err}") from err

    try:
        variable = getattr(module, var_name)
    except AttributeError as err:
        # 属性不存在也归一成 ImportError——对调用方而言「想要的符号拿不到」就是 import 失败。
        raise ImportError(f"模块 {module_path} 未定义 {var_name} 属性/类") from err

    # 类型校验：isinstance 对实例和类对象都用「是否某类的实例」语义。
    if expected_type is not None:
        if not isinstance(variable, expected_type):
            type_name = expected_type.__name__ if isinstance(expected_type, type) else " 或 ".join(t.__name__ for t in expected_type)
            raise ValueError(f"{variable_path} 不是 {type_name} 的实例，得到 {type(variable).__name__}")

    return variable


def resolve_class(class_path: str, base_class: type[T] | None = None) -> type[T]:
    """从 'module.path:ClassName' 字符串动态加载类。

    Args:
        class_path: 类路径字符串。
        base_class: 基类（校验继承关系）。

    Returns:
        类对象（未实例化）。

    Raises:
        ImportError: 路径畸形 / 模块缺失 / 属性不存在（透传 :func:`resolve_variable`）。
        ValueError: 解析得到的不是类，或不是 ``base_class`` 的子类。
    """
    # 先用 expected_type=type 确保拿到的是类对象（类是 ``type`` 的实例）。
    model_class = resolve_variable(class_path, expected_type=type)

    if not isinstance(model_class, type):
        raise ValueError(f"{class_path} 不是一个有效的类")

    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(f"{class_path} 不是 {base_class.__name__} 的子类")
    return model_class


# notes:
# "它是不是一个类"             isinstance(x, type)
# "它是不是某类的实例(实物)"	isinstance(x, 某类)
# "它是不是某类的子类(亲戚)"	issubclass(类, 某类)
