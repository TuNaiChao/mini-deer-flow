"""中间件系统测试"""
from deerflow.agents.middlewares import build_middlewares


def test_middleware_assembly():
    print("正在测试中间件装配...")

    # 构建中间件链
    middlewares = build_middlewares()
    print(f"✓ 装配了 {len(middlewares)} 个中间件")

    for i, mw in enumerate(middlewares):
        print(f"  [{i}] {type(mw).__name__}")

    # 验证 ClarificationMiddleware 在最后
    from deerflow.agents.middlewares.clarification_middleware import (
        ClarificationMiddleware,
    )
    assert isinstance(middlewares[-1], ClarificationMiddleware), \
        "ClarificationMiddleware 必须排在最后！"
    print("✓ ClarificationMiddleware 位置验证通过（排在最后）")

    print("\n✓ 中间件装配测试通过!")


def test_middleware_with_options():
    print("\n正在测试条件装配...")

    # 通过 custom_middlewares 注入一个自定义中间件（插在 Clarification 之前）
    from langchain.agents.middleware import AgentMiddleware


    class MyCustomMiddleware(AgentMiddleware):
        pass

    with_custom = build_middlewares(custom_middlewares=[MyCustomMiddleware()])
    print(f"✓ 带自定义中间件: {len(with_custom)} 个中间件")
    for i, mw in enumerate(with_custom):
        print(f"  [{i}] {type(mw).__name__}")
    assert isinstance(with_custom[-2], MyCustomMiddleware), \
        "自定义中间件应插在最后的 Clarification 之前"

    print("\n✓ 条件装配测试通过!")


if __name__ == "__main__":
    test_middleware_assembly()
    test_middleware_with_options()