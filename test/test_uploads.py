"""M23 uploads —— 文件上传 + markitdown 转换 的 hermetic 测试。

覆盖：路径清洗 / 唯一文件名 / 路径穿越 / symlink 防御（POSIX O_NOFOLLOW + Windows 回退）/
列表 / 删除（伴随 .md 清理）/ 虚拟路径 / per-user per-thread 隔离 / markitdown soft-load /
PDF 双转换策略 / 大纲抽取 / 事件循环内复用 worker / 配置归一。

全 hermetic：物理目录走 ``DEER_FLOW_HOME=tmp_path``；转换器 import 用 monkeypatch 模拟
缺包 / 成功，不依赖真实 markitdown / pymupdf4llm 是否安装。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.config.paths import get_paths
from deerflow.config.uploads_config import UploadsConfig
from deerflow.uploads import (
    CONVERTIBLE_EXTENSIONS,
    PathTraversalError,
    UnsafeUploadPathError,
    claim_unique_filename,
    convert_file_to_markdown,
    convert_with_pool,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
    extract_outline,
    get_uploads_dir,
    list_files_in_dir,
    make_conversion_pool,
    normalize_filename,
    open_upload_file_no_symlink,
    upload_artifact_url,
    upload_virtual_path,
    validate_path_traversal,
    validate_thread_id,
    write_upload_file_no_symlink,
)
from deerflow.uploads import conversion as conversion_mod
from deerflow.uploads import manager as manager_mod


@pytest.fixture
def uploads_dir(tmp_path: Path) -> Path:
    """干净的临时上传目录。"""
    d = tmp_path / "uploads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# normalize_filename
# ---------------------------------------------------------------------------


class TestNormalizeFilename:
    def test_safe_filename(self):
        assert normalize_filename("report.pdf") == "report.pdf"

    def test_strips_path_components(self):
        # 只取 basename——目录成分被剥掉。
        assert normalize_filename("a/b/c.txt") == "c.txt"
        assert normalize_filename("/etc/passwd") == "passwd"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_filename("")

    def test_rejects_dot_dot(self):
        # ".." / "." 单独出现是穿越模式。
        with pytest.raises(ValueError, match="unsafe"):
            normalize_filename("..")
        with pytest.raises(ValueError, match="unsafe"):
            normalize_filename(".")

    def test_strips_separators(self):
        # 末尾分隔符被剥，剩 basename。
        assert normalize_filename("foo.txt/") == "foo.txt"

    def test_rejects_backslash(self):
        # Windows 风格路径——拒反斜杠（Linux 上 Path.name 会保留它当字面字符）。
        with pytest.raises(ValueError, match="backslash"):
            normalize_filename("foo\\bar.txt")

    def test_rejects_too_long(self):
        # 256 字节的 UTF-8 文件名超限。
        long_name = "a" * 256
        with pytest.raises(ValueError, match="too long"):
            normalize_filename(long_name)

    def test_allows_max_length(self):
        # 恰好 255 字节——允许。
        name = "a" * 255
        assert normalize_filename(name) == name

    def test_cjk_byte_length(self):
        # 中文每字 3 字节 UTF-8——85 字 = 255 字节，允许；86 字 = 258 字节，拒绝。
        assert normalize_filename("中" * 85) == "中" * 85
        with pytest.raises(ValueError, match="too long"):
            normalize_filename("中" * 86)


# ---------------------------------------------------------------------------
# claim_unique_filename
# ---------------------------------------------------------------------------


class TestClaimUniqueFilename:
    def test_no_collision(self):
        seen: set[str] = set()
        assert claim_unique_filename("a.txt", seen) == "a.txt"
        assert seen == {"a.txt"}

    def test_single_collision(self):
        seen = {"a.txt"}
        assert claim_unique_filename("a.txt", seen) == "a_1.txt"
        assert seen == {"a.txt", "a_1.txt"}

    def test_triple_collision(self):
        seen = {"a.txt", "a_1.txt", "a_2.txt"}
        assert claim_unique_filename("a.txt", seen) == "a_3.txt"

    def test_mutates_seen(self):
        seen: set[str] = set()
        claim_unique_filename("x.bin", seen)
        claim_unique_filename("x.bin", seen)
        assert seen == {"x.bin", "x_1.bin"}

    def test_preserves_multiple_suffixes(self):
        # .tar.gz —— Path.stem 取 "archive.tar"、.suffix 取 ".gz"，故 _N 插在最后一段前。
        # 与 deer 行为一致（仅处理最后一段扩展名，多段扩展名是已知限制）。
        seen: set[str] = set()
        claim_unique_filename("archive.tar.gz", seen)
        assert claim_unique_filename("archive.tar.gz", seen) == "archive.tar_1.gz"


# ---------------------------------------------------------------------------
# validate_thread_id
# ---------------------------------------------------------------------------


class TestValidateThreadId:
    @pytest.mark.parametrize("tid", ["abc", "thread-1", "thread_2", "t.3", "T-4_5.6", "0123456789"])
    def test_valid(self, tid):
        validate_thread_id(tid)  # 不抛即通过

    @pytest.mark.parametrize("tid", ["", "../etc", "a/b", "a\\b", "a b", "a:b", "a;b"])
    def test_invalid(self, tid):
        with pytest.raises(ValueError, match="Invalid thread_id"):
            validate_thread_id(tid)


# ---------------------------------------------------------------------------
# validate_path_traversal
# ---------------------------------------------------------------------------


class TestValidatePathTraversal:
    def test_inside_base_ok(self, tmp_path):
        p = tmp_path / "sub" / "file.txt"
        p.parent.mkdir()
        p.touch()
        validate_path_traversal(p, tmp_path)  # 不抛即通过

    def test_outside_base_raises(self, tmp_path):
        outside = tmp_path.parent / "escape.txt"
        with pytest.raises(PathTraversalError, match="traversal"):
            validate_path_traversal(outside, tmp_path)

    def test_dotdot_in_path_raises(self, tmp_path):
        # base/x/../../escape 解析后在 base 之外。
        p = tmp_path / "x" / ".." / ".." / "escape.txt"
        with pytest.raises(PathTraversalError):
            validate_path_traversal(p, tmp_path)

    def test_symlink_escape(self, tmp_path):
        # symlink 指向 base 之外——resolve() 跟随它，故被识别为越界。
        target = tmp_path.parent / "secret.txt"
        target.touch()
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        with pytest.raises(PathTraversalError):
            validate_path_traversal(link, tmp_path)


# ---------------------------------------------------------------------------
# open_upload_file_no_symlink / write_upload_file_no_symlink
# ---------------------------------------------------------------------------


class TestOpenUploadFileNoSymlink:
    def test_writes_new_file(self, uploads_dir):
        dest, fh = open_upload_file_no_symlink(uploads_dir, "new.txt")
        with fh:
            fh.write(b"hello")
        assert dest.read_bytes() == b"hello"

    def test_returns_resolved_dest(self, uploads_dir):
        dest, fh = open_upload_file_no_symlink(uploads_dir, "new.bin")
        fh.close()
        assert dest == uploads_dir / "new.bin"

    def test_overwrites_existing_regular_file(self, uploads_dir):
        (uploads_dir / "f.txt").write_bytes(b"OLD")
        dest, fh = open_upload_file_no_symlink(uploads_dir, "f.txt")
        with fh:
            fh.write(b"NEW")
        assert dest.read_bytes() == b"NEW"

    def test_rejects_symlink_destination(self, uploads_dir, tmp_path):
        # symlink 指向 base 外的敏感文件——POSIX O_NOFOLLOW 拒绝。
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("POSIX O_NOFOLLOW 不可用")
        target = tmp_path.parent / "secret.txt"
        target.write_bytes(b"SENSITIVE")
        link = uploads_dir / "evil.txt"
        link.symlink_to(target)
        with pytest.raises(UnsafeUploadPathError):
            open_upload_file_no_symlink(uploads_dir, "evil.txt")
        # 目标文件未被篡改。
        assert target.read_bytes() == b"SENSITIVE"

    def test_rejects_directory_destination(self, uploads_dir):
        (uploads_dir / "subdir").mkdir()
        with pytest.raises((UnsafeUploadPathError, IsADirectoryError)):
            open_upload_file_no_symlink(uploads_dir, "subdir")

    def test_traversal_filename_neutralized_to_basename(self, uploads_dir):
        # normalize_filename 先剥目录成分——"../escape.txt" 被中和成 "escape.txt"，
        # 落在 base 内（不逃逸）。这是第一道防线；validate_path_traversal 是纵深防御。
        dest, fh = open_upload_file_no_symlink(uploads_dir, "../escape.txt")
        with fh:
            fh.write(b"ok")
        assert dest == uploads_dir / "escape.txt"
        assert (uploads_dir / "escape.txt").read_bytes() == b"ok"

    def test_uses_nonblocking_flag_when_available(self, uploads_dir, monkeypatch):
        # O_NOFOLLOW 路径会附加 O_NONBLOCK（若 os 有此常量）。
        seen_flags: list[int] = []
        real_open = os.open

        def spy_open(path, flags, *args, **kwargs):
            seen_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy_open)
        _, fh = open_upload_file_no_symlink(uploads_dir, "nb.txt")
        fh.close()
        assert seen_flags, "os.open 应被调用"
        # 若平台支持 O_NONBLOCK，应被包含进 flags。
        if hasattr(os, "O_NONBLOCK") and hasattr(os, "O_NOFOLLOW"):
            assert os.O_NONBLOCK & seen_flags[0]
            assert os.O_NOFOLLOW & seen_flags[0]

    def test_windows_fallback_succeeds(self, uploads_dir, monkeypatch):
        # 模拟 Windows：删除 os.O_NOFOLLOW，走 lstat+fstat 回退路径。
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
        dest, fh = open_upload_file_no_symlink(uploads_dir, "win.txt")
        with fh:
            fh.write(b"data")
        assert dest.read_bytes() == b"data"

    def test_windows_fallback_rejects_symlink(self, uploads_dir, tmp_path, monkeypatch):
        # Windows 回退路径也应拒绝 symlink（pre-open lstat 检测到非 regular file）。
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
        target = tmp_path.parent / "outside.txt"
        target.write_bytes(b"X")
        link = uploads_dir / "lnk.txt"
        link.symlink_to(target)
        with pytest.raises(UnsafeUploadPathError):
            open_upload_file_no_symlink(uploads_dir, "lnk.txt")


class TestWriteUploadFileNoSymlink:
    def test_writes_bytes(self, uploads_dir):
        dest = write_upload_file_no_symlink(uploads_dir, "blob.bin", b"\x00\x01\x02")
        assert dest.read_bytes() == b"\x00\x01\x02"

    def test_overwrite_via_write_helper(self, uploads_dir):
        write_upload_file_no_symlink(uploads_dir, "o.txt", b"1")
        write_upload_file_no_symlink(uploads_dir, "o.txt", b"22")
        assert (uploads_dir / "o.txt").read_bytes() == b"22"

    def test_rejects_symlink(self, uploads_dir, tmp_path):
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("POSIX O_NOFOLLOW 不可用")
        target = tmp_path.parent / "victim.txt"
        target.write_bytes(b"V")
        (uploads_dir / "h.txt").symlink_to(target)
        with pytest.raises(UnsafeUploadPathError):
            write_upload_file_no_symlink(uploads_dir, "h.txt", b"evil")
        assert target.read_bytes() == b"V"


# ---------------------------------------------------------------------------
# list_files_in_dir
# ---------------------------------------------------------------------------


class TestListFilesInDir:
    def test_empty_dir(self, uploads_dir):
        result = list_files_in_dir(uploads_dir)
        assert result == {"files": [], "count": 0}

    def test_nonexistent_dir(self, tmp_path):
        assert list_files_in_dir(tmp_path / "nope") == {"files": [], "count": 0}

    def test_multiple_files_sorted(self, uploads_dir):
        (uploads_dir / "b.txt").write_bytes(b"bb")
        (uploads_dir / "a.txt").write_bytes(b"a")
        result = list_files_in_dir(uploads_dir)
        names = [f["filename"] for f in result["files"]]
        assert names == ["a.txt", "b.txt"]
        assert result["count"] == 2

    def test_file_entry_fields(self, uploads_dir):
        (uploads_dir / "report.pdf").write_bytes(b"PDF")
        entry = list_files_in_dir(uploads_dir)["files"][0]
        assert set(entry) == {"filename", "size", "path", "extension", "modified"}
        assert entry["filename"] == "report.pdf"
        assert entry["size"] == 3
        assert entry["extension"] == ".pdf"
        assert isinstance(entry["modified"], float)

    def test_ignores_subdirectories(self, uploads_dir):
        (uploads_dir / "file.txt").write_bytes(b"x")
        (uploads_dir / "subdir").mkdir()
        result = list_files_in_dir(uploads_dir)
        assert result["count"] == 1
        assert result["files"][0]["filename"] == "file.txt"

    def test_ignores_symlinks(self, uploads_dir, tmp_path):
        # symlink 不算合法上传文件。
        (uploads_dir / "real.txt").write_bytes(b"r")
        (uploads_dir / "link.txt").symlink_to(tmp_path.parent / "elsewhere.txt")
        result = list_files_in_dir(uploads_dir)
        assert result["count"] == 1
        assert result["files"][0]["filename"] == "real.txt"


# ---------------------------------------------------------------------------
# delete_file_safe
# ---------------------------------------------------------------------------


class TestDeleteFileSafe:
    def test_delete_existing_file(self, uploads_dir):
        (uploads_dir / "gone.txt").write_bytes(b"bye")
        result = delete_file_safe(uploads_dir, "gone.txt")
        assert result["success"] is True
        assert not (uploads_dir / "gone.txt").exists()

    def test_delete_nonexistent_raises(self, uploads_dir):
        with pytest.raises(FileNotFoundError):
            delete_file_safe(uploads_dir, "nope.txt")

    def test_delete_traversal_raises(self, uploads_dir):
        with pytest.raises(PathTraversalError, match="traversal"):
            delete_file_safe(uploads_dir, "../outside.txt")

    def test_companion_md_cleanup(self, uploads_dir):
        # PDF + 其伴随 .md 都在——删 PDF 时顺带删 .md。
        (uploads_dir / "doc.pdf").write_bytes(b"pdf")
        (uploads_dir / "doc.md").write_bytes(b"# md")
        delete_file_safe(uploads_dir, "doc.pdf", convertible_extensions={".pdf"})
        assert not (uploads_dir / "doc.pdf").exists()
        assert not (uploads_dir / "doc.md").exists()

    def test_companion_md_only_for_convertible(self, uploads_dir):
        # .txt 不在 convertible_extensions——删它不动 .md。
        (uploads_dir / "notes.txt").write_bytes(b"n")
        (uploads_dir / "notes.md").write_bytes(b"m")
        delete_file_safe(uploads_dir, "notes.txt", convertible_extensions={".pdf"})
        assert not (uploads_dir / "notes.txt").exists()
        assert (uploads_dir / "notes.md").exists()

    def test_no_convertible_extensions_leaves_md(self, uploads_dir):
        # 不传 convertible_extensions——即使删 PDF 也不清 .md。
        (uploads_dir / "doc.pdf").write_bytes(b"p")
        (uploads_dir / "doc.md").write_bytes(b"m")
        delete_file_safe(uploads_dir, "doc.pdf")
        assert not (uploads_dir / "doc.pdf").exists()
        assert (uploads_dir / "doc.md").exists()

    def test_companion_md_missing_is_ok(self, uploads_dir):
        # PDF 存在但 .md 不存在（转换失败）——missing_ok 不抛错。
        (uploads_dir / "lonely.pdf").write_bytes(b"p")
        delete_file_safe(uploads_dir, "lonely.pdf", convertible_extensions={".pdf"})
        assert not (uploads_dir / "lonely.pdf").exists()


# ---------------------------------------------------------------------------
# enrich_file_listing / virtual paths
# ---------------------------------------------------------------------------


class TestEnrichAndPaths:
    def test_virtual_path(self):
        assert upload_virtual_path("a.pdf") == "/mnt/user-data/uploads/a.pdf"

    def test_artifact_url_percent_encodes(self):
        # 空格 / # / ? 都要 percent-encode。
        url = upload_artifact_url("t-1", "my file #1.pdf")
        assert url == "/api/threads/t-1/artifacts/mnt/user-data/uploads/my%20file%20%231.pdf"

    def test_artifact_url_safe_chars(self):
        # 字母数字 -_.~ 不编码。
        assert upload_artifact_url("t", "a-1_.pdf") == "/api/threads/t/artifacts/mnt/user-data/uploads/a-1_.pdf"

    def test_enrich_adds_fields(self):
        result = {
            "files": [{"filename": "a.pdf", "size": 1, "path": "/p", "extension": ".pdf", "modified": 0.0}],
            "count": 1,
        }
        out = enrich_file_listing(result, "thread-9")
        f = out["files"][0]
        assert f["virtual_path"] == "/mnt/user-data/uploads/a.pdf"
        assert f["artifact_url"] == "/api/threads/thread-9/artifacts/mnt/user-data/uploads/a.pdf"

    def test_enrich_mutates_in_place(self):
        result = {"files": [{"filename": "x.txt"}], "count": 1}
        ret = enrich_file_listing(result, "t")
        assert ret is result  # 同一对象


# ---------------------------------------------------------------------------
# get_uploads_dir / ensure_uploads_dir —— per-user per-thread 隔离
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 DEER_FLOW_HOME 指到临时目录，隔离物理上传路径。"""
    home = tmp_path / "deerflow-home"
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    return home


@pytest.fixture
def known_user(monkeypatch: pytest.MonkeyPatch):
    """固定 user_id，确保 get_uploads_dir 走已知用户。"""
    from deerflow.runtime.user_context import reset_current_user, set_current_user

    token = set_current_user(SimpleNamespace(id="user-42"))
    yield "user-42"
    reset_current_user(token)


class TestUploadsDirResolution:
    def test_get_uploads_dir_layout(self, isolated_home, known_user):
        d = get_uploads_dir("thread-7")
        # per-user per-thread 隔离：{home}/users/{user}/threads/{thread}/user-data/uploads
        assert d == isolated_home / "users" / "user-42" / "threads" / "thread-7" / "user-data" / "uploads"

    def test_get_uploads_dir_no_side_effects(self, isolated_home, known_user):
        d = get_uploads_dir("thread-7")
        assert not d.exists()  # 不建目录

    def test_ensure_uploads_dir_creates(self, isolated_home, known_user):
        d = ensure_uploads_dir("thread-7")
        assert d.is_dir()
        assert d.exists()

    def test_per_user_isolation(self, isolated_home, monkeypatch):
        from deerflow.runtime.user_context import reset_current_user, set_current_user

        token_a = set_current_user(SimpleNamespace(id="alice"))
        dir_a = get_uploads_dir("t1")
        reset_current_user(token_a)
        token_b = set_current_user(SimpleNamespace(id="bob"))
        dir_b = get_uploads_dir("t1")
        reset_current_user(token_b)
        assert dir_a != dir_b
        assert "alice" in str(dir_a) and "bob" in str(dir_b)

    def test_per_thread_isolation(self, isolated_home, known_user):
        d1 = get_uploads_dir("t1")
        d2 = get_uploads_dir("t2")
        assert d1 != d2

    def test_invalid_thread_id_rejected(self, isolated_home, known_user):
        with pytest.raises(ValueError, match="Invalid thread_id"):
            get_uploads_dir("../escape")

    def test_paths_method_matches_local_sandbox_helper(self, isolated_home):
        # _thread_user_data_root 委托 Paths.thread_user_data_dir——同一布局（唯一真相源）。
        from deerflow.sandbox.local.local_sandbox import _thread_user_data_root

        p = get_paths()
        assert _thread_user_data_root("t1", "u1") == p.thread_user_data_dir("u1", "t1")
        assert p.sandbox_uploads_dir("t1", user_id="u1") == p.thread_user_data_dir("u1", "t1") / "uploads"
        # 与上传目录拼接一致。
        assert _thread_user_data_root("t1", "u1") / "uploads" == p.sandbox_uploads_dir("t1", user_id="u1")


# ---------------------------------------------------------------------------
# CONVERTIBLE_EXTENSIONS
# ---------------------------------------------------------------------------


class TestConvertibleExtensions:
    def test_contains_expected_formats(self):
        for ext in (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"):
            assert ext in CONVERTIBLE_EXTENSIONS

    def test_excludes_non_documents(self):
        for ext in (".txt", ".md", ".png", ".csv", ".json", ".html"):
            assert ext not in CONVERTIBLE_EXTENSIONS


# ---------------------------------------------------------------------------
# convert_file_to_markdown —— soft-load + PDF 双转换策略
# ---------------------------------------------------------------------------


class TestConvertFileToMarkdown:
    def test_soft_load_missing_packages_returns_none(self, tmp_path, monkeypatch):
        # 模拟两个转换器都缺包 → convert_file_to_markdown 返回 None，原文件保留。
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: None)
        monkeypatch.setattr(conversion_mod, "_do_convert", _raise_importerror)

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"not a real pdf")
        result = asyncio.run(convert_file_to_markdown(f))
        assert result is None
        assert f.exists()  # 原文件仍在

    def test_writes_md_on_success(self, tmp_path, monkeypatch):
        # _do_convert 返回文本 → 写出伴随 .md。
        monkeypatch.setattr(conversion_mod, "_get_pdf_converter", lambda: "markitdown")
        monkeypatch.setattr(conversion_mod, "_do_convert", lambda p, c: "# Title\n\nbody text")

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"pdf")
        md = asyncio.run(convert_file_to_markdown(f))
        assert md == f.with_suffix(".md")
        assert md.read_text(encoding="utf-8") == "# Title\n\nbody text"

    def test_offloads_large_file_to_thread(self, tmp_path, monkeypatch):
        # 大文件（>1MB）走 asyncio.to_thread。
        f = tmp_path / "big.pdf"
        f.write_bytes(b"x" * (conversion_mod._ASYNC_THRESHOLD_BYTES + 1))
        calls: list[str] = []

        async def spy_to_thread(func, *args, **kwargs):
            calls.append("to_thread")
            return func(*args, **kwargs)

        monkeypatch.setattr(conversion_mod.asyncio, "to_thread", spy_to_thread)
        monkeypatch.setattr(conversion_mod, "_do_convert", lambda p, c: "md")
        asyncio.run(convert_file_to_markdown(f))
        assert calls == ["to_thread"]

    def test_small_file_runs_inline(self, tmp_path, monkeypatch):
        # 小文件（<1MB）同步执行，不走 to_thread。
        f = tmp_path / "small.pdf"
        f.write_bytes(b"x" * 10)
        calls: list[str] = []

        async def spy_to_thread(func, *args, **kwargs):
            calls.append("to_thread")
            return func(*args, **kwargs)

        monkeypatch.setattr(conversion_mod.asyncio, "to_thread", spy_to_thread)
        monkeypatch.setattr(conversion_mod, "_do_convert", lambda p, c: "md")
        asyncio.run(convert_file_to_markdown(f))
        assert calls == []  # 没走 to_thread

    def test_conversion_failure_preserves_original(self, tmp_path, monkeypatch):
        # 转换器抛任意异常 → 返回 None，原文件保留（soft-load 契约）。
        def boom(p, c):
            raise RuntimeError("converter exploded")

        monkeypatch.setattr(conversion_mod, "_do_convert", boom)
        f = tmp_path / "doc.docx"
        f.write_bytes(b"docx")
        assert asyncio.run(convert_file_to_markdown(f)) is None
        assert f.exists()


def _raise_importerror(path):
    raise ImportError("markitdown not installed")


class TestPdfDoubleConverterStrategy:
    def test_auto_uses_pymupdf_when_dense(self, tmp_path, monkeypatch):
        # auto 模式：pymupdf4llm 输出足够长 → 直接用，不碰 markitdown。
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: "X" * 500)
        monkeypatch.setattr(conversion_mod, "_pymupdf_output_too_sparse", lambda t, p: False)
        markitdown_calls: list[Path] = []
        monkeypatch.setattr(conversion_mod, "_convert_with_markitdown", lambda p: markitdown_calls.append(p) or "should not reach")
        out = conversion_mod._do_convert(tmp_path / "a.pdf", "auto")
        assert out == "X" * 500
        assert markitdown_calls == []

    def test_auto_falls_back_when_sparse(self, tmp_path, monkeypatch):
        # auto 模式：pymupdf4llm 输出太稀疏（疑似图片 PDF）→ 回退 markitdown。
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: "sparse")
        monkeypatch.setattr(conversion_mod, "_pymupdf_output_too_sparse", lambda t, p: True)
        monkeypatch.setattr(conversion_mod, "_convert_with_markitdown", lambda p: "OCR result")
        out = conversion_mod._do_convert(tmp_path / "a.pdf", "auto")
        assert out == "OCR result"

    def test_explicit_pymupdf_no_fallback(self, tmp_path, monkeypatch):
        # 显式 pymupdf4llm：即使稀疏也不回退。
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: "sparse")
        monkeypatch.setattr(conversion_mod, "_convert_with_markitdown", lambda p: "fallback")
        out = conversion_mod._do_convert(tmp_path / "a.pdf", "pymupdf4llm")
        assert out == "sparse"

    def test_explicit_markitdown_skips_pymupdf(self, tmp_path, monkeypatch):
        # 显式 markitdown：跳过 pymupdf4llm。
        pymupdf_calls: list[Path] = []
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: pymupdf_calls.append(p) or "x")
        monkeypatch.setattr(conversion_mod, "_convert_with_markitdown", lambda p: "md")
        out = conversion_mod._do_convert(tmp_path / "a.pdf", "markitdown")
        assert out == "md"
        assert pymupdf_calls == []

    def test_non_pdf_uses_markitdown(self, tmp_path, monkeypatch):
        # 非 PDF 一律 markitdown。
        pymupdf_calls: list[Path] = []
        monkeypatch.setattr(conversion_mod, "_convert_pdf_with_pymupdf4llm", lambda p: pymupdf_calls.append(p) or "x")
        monkeypatch.setattr(conversion_mod, "_convert_with_markitdown", lambda p: "md")
        out = conversion_mod._do_convert(tmp_path / "a.docx", "auto")
        assert out == "md"
        assert pymupdf_calls == []


class TestPymupdfSparseDetection:
    def test_dense_text_not_sparse(self, tmp_path):
        assert conversion_mod._pymupdf_output_too_sparse("X" * 2000, tmp_path / "x.pdf") is False

    def test_near_empty_is_sparse(self, tmp_path):
        # 无 pymupdf 可读页数 → 退化为绝对 200 字阈值；空文本判定为稀疏。
        assert conversion_mod._pymupdf_output_too_sparse("", tmp_path / "x.pdf") is True


# ---------------------------------------------------------------------------
# extract_outline
# ---------------------------------------------------------------------------


class TestExtractOutline:
    def test_standard_headings(self, tmp_path):
        md = tmp_path / "doc.md"
        md.write_text("# Intro\n\nbody\n\n## Details\n\nmore\n")
        outline = extract_outline(md)
        titles = [e["title"] for e in outline if "title" in e]
        assert titles == ["Intro", "Details"]
        assert outline[0]["line"] == 1
        assert outline[1]["line"] == 5

    def test_bold_structural_heading(self, tmp_path):
        md = tmp_path / "doc.md"
        md.write_text("**ITEM 1. BUSINESS**\n\nsome text\n")
        outline = extract_outline(md)
        assert any(e.get("title") == "ITEM 1. BUSINESS" for e in outline)

    def test_split_bold_heading(self, tmp_path):
        md = tmp_path / "doc.md"
        md.write_text("**1** **Introduction**\n\nbody\n")
        outline = extract_outline(md)
        assert any(e.get("title") == "1 Introduction" for e in outline)

    def test_truncates_at_max_entries(self, tmp_path):
        md = tmp_path / "big.md"
        md.write_text("".join(f"# Heading {i}\n\n" for i in range(conversion_mod.MAX_OUTLINE_ENTRIES + 5)))
        outline = extract_outline(md)
        # 前 MAX 个 + 1 个 truncated 哨兵。
        assert len(outline) == conversion_mod.MAX_OUTLINE_ENTRIES + 1
        assert outline[-1] == {"truncated": True}

    def test_empty_file(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("")
        assert extract_outline(md) == []

    def test_unreadable_returns_empty(self, tmp_path):
        assert extract_outline(tmp_path / "nonexistent.md") == []


# ---------------------------------------------------------------------------
# make_conversion_pool / convert_with_pool —— 事件循环内复用 worker
# ---------------------------------------------------------------------------


class TestConversionPool:
    def test_no_pool_outside_event_loop(self):
        # 不在事件循环里 → None（直接 asyncio.run 即可）。
        assert make_conversion_pool() is None

    def test_pool_inside_event_loop(self):
        # 在活动事件循环里 → 返回单 worker 线程池。

        async def check():
            pool = make_conversion_pool()
            assert pool is not None
            assert pool._max_workers == 1
            pool.shutdown(wait=True)

        asyncio.run(check())

    def test_convert_with_pool_none(self, monkeypatch):
        # pool=None → 直接 asyncio.run(convert_file_to_markdown)。
        called: list[Path] = []

        async def fake_convert(path):
            called.append(path)
            return path.with_suffix(".md")

        monkeypatch.setattr(manager_mod, "convert_file_to_markdown", fake_convert)
        p = Path("/tmp/x.pdf")
        result = convert_with_pool(None, p)
        assert result == Path("/tmp/x.md")
        assert called == [p]

    def test_convert_with_pool_reuses_worker(self, monkeypatch):
        # pool 非 None → 提交到 worker 线程（worker 内 asyncio.run）。

        async def check():
            pool = make_conversion_pool()
            assert pool is not None

            thread_ids: set[int] = set()

            async def fake_convert(path):
                import threading

                thread_ids.add(threading.get_ident())
                return path.with_suffix(".md")

            monkeypatch.setattr(manager_mod, "convert_file_to_markdown", fake_convert)

            results = [convert_with_pool(pool, Path(f"/tmp/{i}.pdf")) for i in range(3)]
            pool.shutdown(wait=True)
            return results, thread_ids

        results, thread_ids = asyncio.run(check())
        assert results == [Path("/tmp/0.md"), Path("/tmp/1.md"), Path("/tmp/2.md")]
        # 所有转换跑在同一个 worker 线程里（复用），且不是主线程。
        assert len(thread_ids) == 1

    def test_convert_with_pool_inside_loop_works(self, monkeypatch):
        # 验证：在活动循环里用 pool 转换不抛 RuntimeError（不直接 asyncio.run）。

        async def fake_convert(path):
            return path.with_suffix(".md")

        monkeypatch.setattr(manager_mod, "convert_file_to_markdown", fake_convert)

        async def run():
            pool = make_conversion_pool()
            try:
                return convert_with_pool(pool, Path("/tmp/a.pdf"))
            finally:
                pool.shutdown(wait=True)

        assert asyncio.run(run()) == Path("/tmp/a.md")


# ---------------------------------------------------------------------------
# UploadsConfig
# ---------------------------------------------------------------------------


class TestUploadsConfig:
    def test_defaults(self):
        cfg = UploadsConfig()
        assert cfg.auto_convert_documents is True
        assert cfg.pdf_converter == "auto"

    def test_normalized_pdf_converter_valid(self):
        assert UploadsConfig(pdf_converter="pymupdf4llm").normalized_pdf_converter() == "pymupdf4llm"
        assert UploadsConfig(pdf_converter="markitdown").normalized_pdf_converter() == "markitdown"

    def test_normalized_pdf_converter_uppercase(self):
        assert UploadsConfig(pdf_converter="AUTO").normalized_pdf_converter() == "auto"
        assert UploadsConfig(pdf_converter="MarkItDown").normalized_pdf_converter() == "markitdown"

    def test_normalized_pdf_converter_invalid_falls_back(self):
        assert UploadsConfig(pdf_converter="bogus").normalized_pdf_converter() == "auto"
        assert UploadsConfig(pdf_converter="").normalized_pdf_converter() == "auto"

    def test_app_config_has_uploads_field(self, monkeypatch, tmp_path):
        # AppConfig 默认带 uploads 配置。
        from deerflow.config.app_config import AppConfig

        cfg = AppConfig()
        assert cfg.uploads.auto_convert_documents is True


# ---------------------------------------------------------------------------
# 集成：上传 → 列表 → 删除 全流程
# ---------------------------------------------------------------------------


class TestUploadWorkflow:
    def test_full_workflow(self, isolated_home, known_user, tmp_path):
        # 写文件 → 列表（含 enrich）→ 删除（含伴随清理）。
        uploads = ensure_uploads_dir("thread-flow")
        write_upload_file_no_symlink(uploads, "a.pdf", b"PDF content")
        write_upload_file_no_symlink(uploads, "b.txt", b"text")

        # 列表
        listing = list_files_in_dir(uploads)
        assert listing["count"] == 2
        enriched = enrich_file_listing(listing, "thread-flow")
        names = {f["filename"] for f in enriched["files"]}
        assert names == {"a.pdf", "b.txt"}
        assert all("virtual_path" in f and "artifact_url" in f for f in enriched["files"])

        # 删 PDF（带伴随 .md 清理，即使 .md 不存在也不报错）
        write_upload_file_no_symlink(uploads, "a.md", b"# converted")
        delete_file_safe(uploads, "a.pdf", convertible_extensions=set(CONVERTIBLE_EXTENSIONS))
        assert not (uploads / "a.pdf").exists()
        assert not (uploads / "a.md").exists()  # 伴随 .md 一并清掉
        assert (uploads / "b.txt").exists()  # 其余不动

    def test_duplicate_filename_claim_in_batch(self, isolated_home, known_user):
        # 模拟批量上传同名文件——claim_unique_filename 保证不互相覆盖。
        uploads = ensure_uploads_dir("thread-dup")
        seen: set[str] = set()
        names = [claim_unique_filename("report.pdf", seen) for _ in range(3)]
        assert names == ["report.pdf", "report_1.pdf", "report_2.pdf"]
        for name in names:
            write_upload_file_no_symlink(uploads, name, b"content")
        listing = list_files_in_dir(uploads)
        assert listing["count"] == 3
