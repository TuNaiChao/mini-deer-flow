"""文件上传子系统（M23）。

- :mod:`deerflow.uploads.manager` —— 路径安全 + symlink 防御 + 列表 / 删除 + 转换编排。
- :mod:`deerflow.uploads.conversion` —— markitdown 文档 → markdown 转换（soft-load）。
"""

from deerflow.uploads.conversion import (
    CONVERTIBLE_EXTENSIONS,
    convert_file_to_markdown,
    extract_outline,
)
from deerflow.uploads.manager import (
    PathTraversalError,
    UnsafeUploadPathError,
    claim_unique_filename,
    convert_with_pool,
    delete_file_safe,
    enrich_file_listing,
    ensure_uploads_dir,
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

__all__ = [
    # manager
    "get_uploads_dir",
    "ensure_uploads_dir",
    "normalize_filename",
    "claim_unique_filename",
    "validate_path_traversal",
    "validate_thread_id",
    "PathTraversalError",
    "UnsafeUploadPathError",
    "open_upload_file_no_symlink",
    "write_upload_file_no_symlink",
    "list_files_in_dir",
    "delete_file_safe",
    "upload_artifact_url",
    "upload_virtual_path",
    "enrich_file_listing",
    "make_conversion_pool",
    "convert_with_pool",
    # conversion
    "CONVERTIBLE_EXTENSIONS",
    "convert_file_to_markdown",
    "extract_outline",
]
