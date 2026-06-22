"""沙箱配置。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VolumeMountConfig(BaseModel):
    """一个卷挂载的配置。"""

    host_path: str = Field(
        description=(
            "挂载的源路径。``LocalSandboxProvider`` 从 gateway 进程视角检查此路径"
            "（make dev 时是宿主机，Docker 部署时是 gateway 容器内路径，需把宿主目录 bind-mount 进 gateway）；"
            "``AioSandboxProvider``（DooD）把此值直接传给 ``docker -v``，由宿主 Docker daemon 解析。"
        ),
    )
    container_path: str = Field(description="容器内的路径")
    read_only: bool = Field(default=False, description="是否只读挂载")


class SandboxConfig(BaseModel):
    """沙箱配置节。

    通用选项：
        use: 沙箱 provider 的类路径（必填）
        allow_host_bash: 为 LocalSandboxProvider 启用宿主机 bash 执行。危险，
            仅用于完全可信的本地工作流。

    AioSandboxProvider 专属选项：image / port / replicas / container_prefix /
    idle_timeout / mounts / environment / provisioner_url（设了用 K8s 远端 backend，
    不设用本地 Docker/Apple Container backend）。
    """

    use: str = Field(
        description="沙箱 provider 的类路径（如 deerflow.sandbox.local:LocalSandboxProvider）",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="使用 LocalSandboxProvider 时允许 bash 工具直接在宿主机执行。危险；仅用于完全可信的本地环境。",
    )
    image: str | None = Field(default=None, description="沙箱容器用的 Docker 镜像")
    port: int | None = Field(default=None, description="沙箱容器的基础端口")
    replicas: int | None = Field(
        default=None,
        description="最大并发沙箱容器数（默认 3）。达到上限时淘汰最久未用的沙箱腾位。",
    )
    container_prefix: str | None = Field(default=None, description="容器名前缀")
    idle_timeout: int | None = Field(
        default=None,
        description="沙箱释放前的空闲秒数（默认 600 = 10 分钟）。设 0 禁用。",
    )
    provisioner_url: str | None = Field(
        default=None,
        description=("远端 provisioner 服务 URL（如 http://provisioner:8002）。设置后 AioSandboxProvider 用 RemoteSandboxBackend（K8s 动态建 Pod），否则用 LocalContainerBackend（本地 Docker/Apple Container）。"),
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="在宿主机与容器间共享目录的卷挂载列表",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="注入沙箱容器的环境变量。$ 开头的值从宿主环境变量解析。",
    )
    bash_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="bash 工具输出最多保留的字符数。超限则中间截断（保留首尾）。设 0 禁用截断。",
    )
    read_file_output_max_chars: int = Field(
        default=50000,
        ge=0,
        description="read_file 工具输出最多保留的字符数。超限则头部截断。设 0 禁用。",
    )
    ls_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="ls 工具输出最多保留的字符数。超限则头部截断。设 0 禁用。",
    )

    model_config = ConfigDict(extra="allow")
