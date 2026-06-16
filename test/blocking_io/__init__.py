"""blocking-IO gate 回归测试目录。

本目录下每个测试都在严格 ``detect_blocking_io_strict`` 上下文中运行（见
conftest.py）。目的是**锁住**生产代码里必须经 ``asyncio.to_thread`` 卸载的阻塞 IO：
一旦有人删掉了某处 ``to_thread`` 包装，gate 会抛 ``BlockingError`` 让测试失败。

opt-out：在测试上标注 ``@pytest.mark.allow_blocking_io`` 可跳过 gate。
"""
