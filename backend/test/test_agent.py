"""
Agent 最小闭环测试

验证：创建 Agent → 发送消息 → 收到 DeepSeek 回复
"""
from deerflow.agents import make_lead_agent


def test_agent():
    """测试 Agent 基本对话"""
    print("=" * 50)
    print("正在创建 Agent...")

    # 使用运行时配置
    config = {
        "configurable": {
            "thread_id": "test-thread-1",
            "model_name": "deepseek",  # 使用 DeepSeek
        }
    }

    # 创建 Agent（返回 CompiledStateGraph）
    agent = make_lead_agent(config)

    print("✓ Agent 创建成功")
    print(f"  Agent 类型: {type(agent).__name__}")
    print("=" * 50)

    # 测试消息
    test_messages = [
        "你好！请用中文简单介绍一下你自己。",
        "1+1等于几？",
    ]

    for msg in test_messages:
        print(f"\n👤 用户: {msg}")
        print("-" * 50)

        try:
            # invoke 方法接收 {"messages": [...]}
            result = agent.invoke(
                {"messages": [{"role": "user", "content": msg}]},
                config=config,
            )

            # 提取最后一条 AI 回复
            messages = result.get("messages", [])
            if messages:
                last_msg = messages[-1]
                content = getattr(last_msg, "content", str(last_msg))
                print(f"🤖 Agent: {content[:500]}...")
            else:
                print("⚠ 未收到回复")

        except Exception as e:
            print(f"❌ 错误: {e}")

    print("\n" + "=" * 50)
    print("✓ Agent 最小闭环测试完成!")
    print("=" * 50)


if __name__ == "__main__":
    test_agent()