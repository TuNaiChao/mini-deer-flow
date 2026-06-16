"""带中间件的 Agent 测试"""
from deerflow.agents import make_lead_agent


def test_agent_with_middlewares():
    print("=" * 50)
    print("正在创建带中间件的 Agent...")

    config = {
        "configurable": {
            "thread_id": "test-thread-1",
            "model_name": "deepseek",
        }
    }

    agent = make_lead_agent(config)
    print("✓ Agent 创建成功")

    # 测试对话
    test_messages = [
        "你好！我叫小明，是一名程序员。",
        "你还记得我的名字和职业吗？",
    ]

    for msg in test_messages:
        print(f"\n👤 用户: {msg}")
        result = agent.invoke(
            {"messages": [{"role": "user", "content": msg}]},
            config=config,
        )

        messages = result.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content = getattr(last_msg, "content", str(last_msg))
            print(f"🤖 Agent: {content[:300]}...")

        # 检查状态
        title = result.get("title")
        if title:
            print(f"📝 标题: {title}")

    print("\n✓ 带中间件的 Agent 测试完成!")


if __name__ == "__main__":
    test_agent_with_middlewares()