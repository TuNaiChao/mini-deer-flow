"""上传管理逻辑（M23 uploads）——纯业务逻辑，无 FastAPI / HTTP 依赖。

Gateway（M16 UploadsMiddleware / 未来 router）与 Client 都委托本模块的函数。
本模块做三件事：

1. **路径安全**：``normalize_filename``（只取 basename、拒 ``..`` / ``\\``、255 UTF-8 字节上限）
   + ``validate_path_traversal``（``resolve().relative_to()`` 校验越界）+ ``validate_thread_id``
   （thread_id 只允许 ``[A-Za-z0-9._-]``，防 ``../`` 注入路径）。
2. **symlink 防御**：``open_upload_file_no_symlink`` 用 POSIX ``O_NOFOLLOW``（Windows 退化为
   ``lstat`` + ``fstat`` 双校验）拒绝 symlink 目标——防沙箱进程预置 symlink 把上传写入
   「越界文件」（用 gateway 的权限越权）。这是 M23 的核心安全红线（#29）。
3. **转换编排**：``convert_with_pool`` / ``make_conversion_pool`` 实现「事件循环内复用 worker」
   ——在活动事件循环里（如 UploadsMiddleware.abefore_agent）逐文件 ``asyncio.run`` 会反复
   建拆事件循环，改用单 worker 线程复用。

虚拟 ↔ 物理路径映射：agent 看到的是 ``/mnt/user-data/uploads/<file>``（沙箱虚拟路径），
物理上落在 ``{base_dir}/users/{user_id}/threads/{thread_id}/user-data/uploads/<file>``
（per-user per-thread 隔离，见 :class:`deerflow.config.paths.Paths.sandbox_uploads_dir`）。
``upload_virtual_path`` / ``upload_artifact_url`` 构造虚拟路径 / 下载 URL。
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

from deerflow.config.paths import VIRTUAL_PATH_PREFIX, get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.uploads.conversion import convert_file_to_markdown


class PathTraversalError(ValueError):
    """路径越界：路径解析后跑到了允许的基础目录之外。"""


class UnsafeUploadPathError(ValueError):
    """上传目标不是安全的普通文件路径（是 symlink / 目录 / 多硬链接 等）。"""


# thread_id 只允许字母 / 数字 / 点 / 下划线 / 横线——防 ``../`` 或路径分隔符注入文件系统路径。
_SAFE_THREAD_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_thread_id(thread_id: str) -> None:
    """拒绝含文件系统不安全字符的 thread_id。

    Raises:
        ValueError: thread_id 为空或含不安全字符。
    """
    if not thread_id or not _SAFE_THREAD_ID.match(thread_id):
        raise ValueError(f"Invalid thread_id: {thread_id!r}")


def get_uploads_dir(thread_id: str) -> Path:
    """返回某线程的上传目录路径（**无副作用**，不建目录）。"""
    validate_thread_id(thread_id)
    return get_paths().sandbox_uploads_dir(thread_id, user_id=get_effective_user_id())


def ensure_uploads_dir(thread_id: str) -> Path:
    """返回某线程的上传目录，按需创建（含父目录）。"""
    base = get_uploads_dir(thread_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def normalize_filename(filename: str) -> str:
    """把文件名清洗成安全的 basename。

    剥掉任何目录成分，并拒绝穿越模式。

    Args:
        filename: 用户输入的原始文件名（可能含路径成分）。

    Returns:
        安全文件名（仅 basename）。

    Raises:
        ValueError: 文件名为空，或解析为穿越模式（``.`` / ``..``），或含反斜杠
            （Linux 下 ``Path.name`` 会把 ``\\`` 当字面字符保留，但它暗示一个
            Windows 风格路径，应剥离 / 拒绝），或 UTF-8 编码超过 255 字节。
    """
    if not filename:
        raise ValueError("Filename is empty")
    safe = Path(filename).name
    if not safe or safe in {".", ".."}:
        raise ValueError(f"Filename is unsafe: {filename!r}")
    # 拒反斜杠——Linux 上 Path.name 把它当字面字符保留，但它暗示 Windows 风格路径。
    if "\\" in safe:
        raise ValueError(f"Filename contains backslash: {filename!r}")
    if len(safe.encode("utf-8")) > 255:
        raise ValueError(f"Filename too long: {len(safe)} chars")
    return safe


def claim_unique_filename(name: str, seen: set[str]) -> str:
    """碰撞时追加 ``_N`` 后缀生成唯一文件名。

    自动把返回名加入 ``seen``，调用方无需手动加。

    场景：单次上传请求里多个文件同名——直接落盘后写的会截断先写的。``file.txt`` → ``file_1.txt``。

    Args:
        name: 候选文件名。
        seen: 已占用的文件名集合（就地修改）。

    Returns:
        一个不在 ``seen`` 中的文件名（已加入 ``seen``）。
    """
    if name not in seen:
        seen.add(name)
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    candidate = f"{stem}_{counter}{suffix}"
    while candidate in seen:
        counter += 1
        candidate = f"{stem}_{counter}{suffix}"
    seen.add(candidate)
    return candidate


def validate_path_traversal(path: Path, base: Path) -> None:
    """校验 ``path`` 在 ``base`` 之内。

    Raises:
        PathTraversalError: 检测到路径穿越。
    """
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        raise PathTraversalError("Path traversal detected") from None


def open_upload_file_no_symlink(base_dir: Path, filename: str) -> tuple[Path, object]:
    """为安全的流式写入打开上传目标文件。

    上传目录可能被挂进本地沙箱。沙箱进程因此能在一个未来的上传文件名处预置一个 symlink。
    普通 ``Path.write_bytes`` 会**跟随**那个链接，于是可能用 gateway 的权限覆写到上传目录
    **之外**的文件。本 helper 用 POSIX ``O_NOFOLLOW`` 拒绝 symlink 目标。Windows（无
    ``O_NOFOLLOW``）用 ``open()`` 前后的双重 ``lstat`` + ``fstat`` 校验缩小 TOCTOU 窗口；
    这不能根除竞态但显著提高利用难度。路径穿越校验在两种情况下都阻止逃出 ``base_dir``。

    Returns:
        ``(dest_path, file_handle)`` —— file_handle 以 ``"wb"`` 打开，调用方负责关闭。

    Raises:
        UnsafeUploadPathError: 目标是 symlink / 目录 / 多硬链接 等不安全路径。
        PathTraversalError: 解析后逃出 ``base_dir``。
    """
    safe_name = normalize_filename(filename)
    dest = base_dir / safe_name

    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        st = None

    if st is not None and not stat.S_ISREG(st.st_mode):
        raise UnsafeUploadPathError(f"Upload destination is not a regular file: {safe_name}")

    validate_path_traversal(dest, base_dir)

    has_nofollow = hasattr(os, "O_NOFOLLOW")

    if has_nofollow:
        # POSIX：O_NOFOLLOW 让 open() 在 dest 是 symlink 时以 ELOOP 失败。
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK

        try:
            fd = os.open(dest, flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO, errno.EAGAIN}:
                raise UnsafeUploadPathError(f"Unsafe upload destination: {safe_name}") from exc
            raise

        try:
            opened_stat = os.fstat(fd)
            # nlink != 1：目标被多硬链接指向（可能有人故意加链接到敏感文件）。
            if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
                raise UnsafeUploadPathError(f"Upload destination is not an exclusive regular file: {safe_name}")
            os.ftruncate(fd, 0)
            fh = os.fdopen(fd, "wb")
            fd = -1
        finally:
            if fd >= 0:
                os.close(fd)
        return dest, fh

    # Windows：无 O_NOFOLLOW。open() 前再 lstat 一次缩小 TOCTOU 窗口，open() 后 fstat 再校验。
    # 注意：pre-open lstat 与 open() 间仍有一窄竞态；路径穿越校验能缓解逃出 base_dir，但挡不住
    # 攻击者在检查后原子地把 dest 换成 symlink 的情况。
    if st is not None and st.st_nlink > 1:
        raise UnsafeUploadPathError(f"Upload destination has multiple links: {safe_name}")

    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    try:
        pre_open_st = os.lstat(dest)
    except FileNotFoundError:
        pre_open_st = None

    if pre_open_st is not None and not stat.S_ISREG(pre_open_st.st_mode):
        raise UnsafeUploadPathError(f"Upload destination is not a regular file: {safe_name}")
    if pre_open_st is not None and pre_open_st.st_nlink > 1:
        raise UnsafeUploadPathError(f"Upload destination has multiple links: {safe_name}")

    try:
        fd = os.open(dest, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.EISDIR, errno.ENOTDIR, errno.ENXIO, errno.EAGAIN}:
            raise UnsafeUploadPathError(f"Unsafe upload destination: {safe_name}") from exc
        raise

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink > 1:
            raise UnsafeUploadPathError(f"Upload destination is not an exclusive regular file: {safe_name}")
        os.ftruncate(fd, 0)
        fh = os.fdopen(fd, "wb")
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    return dest, fh


def write_upload_file_no_symlink(base_dir: Path, filename: str, data: bytes) -> Path:
    """写入上传字节，拒绝跟随已存在的目标 symlink（流式写）。"""
    dest, fh = open_upload_file_no_symlink(base_dir, filename)
    with fh:
        fh.write(data)
    return dest


def list_files_in_dir(directory: Path) -> dict:
    """列出 ``directory`` 下的**文件**（不含子目录）。

    Args:
        directory: 待扫描目录。

    Returns:
        ``{"files": [...], "count": N}``，files 按名排序。每项含 ``size``（int 字节）。
        调 :func:`enrich_file_listing` 补 virtual / artifact URL。目录不存在返回空列表。
    """
    if not directory.is_dir():
        return {"files": [], "count": 0}

    files = []
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            # follow_symlinks=False：不跟随 symlink（上传目录里的 symlink 不算合法上传文件）。
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            files.append(
                {
                    "filename": entry.name,
                    "size": st.st_size,
                    "path": entry.path,
                    "extension": Path(entry.name).suffix,
                    "modified": st.st_mtime,
                }
            )
    return {"files": files, "count": len(files)}


def delete_file_safe(base_dir: Path, filename: str, *, convertible_extensions: set[str] | None = None) -> dict:
    """经路径穿越校验后删除 ``base_dir`` 内的一个文件。

    若给了 ``convertible_extensions`` 且文件扩展名命中，则顺带删除上传转换时生成的
    伴随 ``.md`` 文件（若存在）——防删了原文留个孤儿 markdown。

    Args:
        base_dir: 文件所在目录。
        filename: 待删文件名。
        convertible_extensions: 小写扩展名集合（如 ``{".pdf", ".docx"}``），其伴随
            markdown 需一并清理。

    Returns:
        ``{"success": True, "message": ...}``。

    Raises:
        FileNotFoundError: 文件不存在。
        PathTraversalError: 检测到路径穿越。
    """
    file_path = (base_dir / filename).resolve()
    validate_path_traversal(file_path, base_dir)

    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {filename}")

    file_path.unlink()

    # 清理上传转换时生成的伴随 markdown。
    if convertible_extensions and file_path.suffix.lower() in convertible_extensions:
        file_path.with_suffix(".md").unlink(missing_ok=True)

    return {"success": True, "message": f"Deleted {filename}"}


def upload_artifact_url(thread_id: str, filename: str) -> str:
    """构造线程上传目录里某文件的 artifact 下载 URL。

    ``filename`` 做 percent-encoding，使空格、``#``、``?`` 等安全。
    """
    return f"/api/threads/{thread_id}/artifacts{VIRTUAL_PATH_PREFIX}/uploads/{quote(filename, safe='')}"


def upload_virtual_path(filename: str) -> str:
    """构造上传目录里某文件的虚拟路径（agent 在沙箱内看到的路径）。"""
    return f"{VIRTUAL_PATH_PREFIX}/uploads/{filename}"


def enrich_file_listing(result: dict, thread_id: str) -> dict:
    """在 list 结果上补 virtual_path / artifact_url（就地修改并返回）。"""
    for f in result["files"]:
        filename = f["filename"]
        f["virtual_path"] = upload_virtual_path(filename)
        f["artifact_url"] = upload_artifact_url(thread_id, filename)
    return result


# ---------------------------------------------------------------------------
# markitdown 转换编排：「事件循环内复用 worker」
# ---------------------------------------------------------------------------
# 上传流程（M16 UploadsMiddleware / Client.upload_files）可能跑在活动事件循环里。逐文件
# ``asyncio.run(convert_file_to_markdown(path))`` 每次都新建+拆除一个事件循环——开销大且
# 在已有循环里 ``asyncio.run`` 直接抛 RuntimeError。本段提供：
#   make_conversion_pool() —— 在活动循环里返回单 worker 线程池，否则返回 None；
#   convert_with_pool(pool, path) —— 有池就提交到 worker（worker 内各自 asyncio.run），
#       无池就直接 asyncio.run。调用方用 ``with`` 或 finally 关池。


def make_conversion_pool():
    """在活动事件循环里返回单 worker 线程池，否则返回 ``None``。

    ``max_workers=1``：转换是顺序的、且单文件转换内部已有线程卸载（>1MB 走 to_thread），
    多 worker 收益小还抢资源。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 不在事件循环里（如同步 CLI）——直接 asyncio.run 即可，无需线程池。
        return None
    import concurrent.futures

    return concurrent.futures.ThreadPoolExecutor(max_workers=1)


def _convert_in_worker(path: Path):
    """worker 线程入口：在自己的事件循环里跑转换。"""
    return asyncio.run(convert_file_to_markdown(path))


def convert_with_pool(pool, path: Path):
    """用（可选的）线程池把一个文件转成 markdown。

    - ``pool`` 非 None（在活动事件循环里）：提交到 worker 线程，worker 内 ``asyncio.run``。
    - ``pool`` 为 None（不在事件循环里）：直接 ``asyncio.run``。
    """
    if pool is not None:
        return pool.submit(_convert_in_worker, path).result()
    return asyncio.run(convert_file_to_markdown(path))
