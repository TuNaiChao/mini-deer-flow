"""工具系统测试"""
from deerflow.tools import get_available_tools


def test_tools():
    print("正在测试工具系统...")

    tools = get_available_tools()
    print(f"✓ 获取到 {len(tools)} 个工具")

    for t in tools:
        desc = (t.description or "")[:60]
        print(f"  - {t.name}: {desc}...")

    # 内置工具必须存在
    names = {t.name for t in tools}
    assert "ask_clarification" in names, "缺少内置工具 ask_clarification"
    assert "present_files" in names, "缺少内置工具 present_files"
    print("\n✓ 工具系统测试通过!")


def test_clarification_invocation():
    print("\n正在测试 ask_clarification 调用...")
    from deerflow.tools.builtins import ask_clarification_tool

    # @tool 装饰的工具，invoke 时传完整参数字典
    result = ask_clarification_tool.invoke({
        "question": "需要哪个文件？",
        "clarification_type": "missing_info",
        "context": "用户没有指定目标文件",
    })
    print(f"✓ ask_clarification 返回: {result}")


if __name__ == "__main__":
    test_tools()
    test_clarification_invocation()