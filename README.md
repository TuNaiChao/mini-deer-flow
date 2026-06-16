# mini-deer-flow

uv venv
uv sync --no-install-project

source .venv/bin/activate


todo:
skill的部分需要补充，怎么实现skills的加载，与tool流程的差异

phase2-步骤 7：把工具接入 Agent：
from deerflow.tools import get_available_tools  # 加到文件顶部 import -> 会有循环引用

phase2-已把工具改为@tool实现，修改对应的描述

phase0-修改了get_env_file的实现

