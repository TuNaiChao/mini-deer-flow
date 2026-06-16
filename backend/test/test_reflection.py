"""动态类加载测试"""
from deerflow.reflection import resolve_class
from langchain_core.language_models import BaseChatModel


def test_resolve_class():
    print("正在测试动态类加载...")

    # 测试加载 DeepSeek
    cls = resolve_class(
        "langchain_deepseek:ChatDeepSeek",
        base_class=BaseChatModel
    )
    print(f"✓ 加载类: {cls.__name__}")

    # 验证确实是 BaseChatModel 的子类
    assert issubclass(cls, BaseChatModel)
    print("✓ 验证通过：是 BaseChatModel 的子类")

    print("\n✓ 动态加载测试通过!")


if __name__ == "__main__":
    test_resolve_class()