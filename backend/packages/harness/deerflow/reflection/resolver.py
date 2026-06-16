"""
动态解析模块

通过 'module.path:ClassName' 格式的字符串动态加载类，
避免硬编码 import，实现完全配置驱动。
"""
import importlib
from typing import TypeVar

T = TypeVar("T")

# 模块名 → pip 包名的提示映射，用于提供友好的安装提示
MODULE_TO_PACKAGE_HINTS = {
    "langchain_openai": "langchain-openai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_deepseek": "langchain-deepseek",
    "langchain_ollama": "langchain-ollama",
}


def resolve_variable(path: str, expected_type: type[T] | None = None) -> T:
    """
    从 'module.path:variable_name' 字符串动态加载变量/类

    Args:
        path: 模块路径和变量名，用 ':' 分隔
        expected_type: 期望的类型（用于验证）

    Returns:
        解析后的变量/类

    Raises:
        ImportError: 模块不存在
        TypeError: 类型不匹配
    """
    module_path, var_name = path.rsplit(":", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        # 提供友好的安装提示
        hint = MODULE_TO_PACKAGE_HINTS.get(module_path)
        if hint:
            raise ImportError(
                f"缺少依赖 '{hint}'。请运行: uv add {hint}"
            ) from e
        raise

    obj = getattr(module, var_name)

    if expected_type is not None:
        # 对于类，使用 issubclass；对于实例，使用 isinstance
        if isinstance(obj, type):
            if not issubclass(obj, expected_type):
                raise TypeError(
                    f"{obj.__name__} 不是 {expected_type.__name__} 的子类"
                )
        elif not isinstance(obj, expected_type):
            raise TypeError(
                f"{type(obj).__name__} 不是 {expected_type.__name__} 的实例"
            )

    return obj


def resolve_class(path: str, base_class: type[T] | None = None) -> type[T]:
    """
    从 'module.path:ClassName' 字符串动态加载类

    Args:
        path: 类路径字符串
        base_class: 基类（用于验证继承关系）

    Returns:
        类对象（未实例化）
    """
    cls = resolve_variable(path)
    # 确保解析得到的确实是一个类（而非实例或函数）
    if not isinstance(cls, type):
        raise TypeError(
            f"{path} 解析得到的不是类，而是 {type(cls).__name__}"
        )
    if base_class is not None and not issubclass(cls, base_class):
        raise TypeError(
            f"{cls.__name__} 不是 {base_class.__name__} 的子类"
        )
    return cls


# notes:
# "它是不是一个类"             isinstance(x, type) 
# "它是不是某类的实例(实物)"	isinstance(x, 某类)
# "它是不是某类的子类(亲戚)"	issubclass(类, 某类)