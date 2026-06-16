"""配置加载测试"""
from deerflow.config import get_app_config, ModelConfig


def test_config_loading():
    print("正在测试配置加载...")

    config = get_app_config()

    print(f"✓ 日志级别: {config.log_level}")
    print(f"✓ 配置的模型数量: {len(config.models)}")

    for model in config.models:
        print(f"  - {model.name}: {model.use} → {model.model}")
        print(f"    支持思考: {model.supports_thinking}")
        print(f"    支持视觉: {model.supports_vision}")

    if not config.models:
        print("⚠ 警告：未配置模型，请先复制 config.example.yaml 为 config.yaml 并设置 API Key")
    else:
        print("\n✓ 配置加载测试通过!")


if __name__ == "__main__":
    test_config_loading()