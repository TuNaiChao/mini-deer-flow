"""模型工厂测试"""
from deerflow.models import create_chat_model, get_default_model


def test_model_factory():
    print("正在测试模型工厂...")

    # 测试默认模型
    model = get_default_model()
    print(f"✓ 默认模型: {type(model).__name__}")

    # 测试指定 DeepSeek 模型
    model = create_chat_model("deepseek")
    print(f"✓ DeepSeek 模型: {type(model).__name__}")

    # 简单调用测试
    response = model.invoke("用一句话介绍你自己")
    print(f"✓ 模型调用成功: {response.content[:80]}...")

    print("\n✓ 模型工厂测试通过!")


if __name__ == "__main__":
    test_model_factory()