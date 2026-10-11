"""Only canonical export names directly inside the configured managed directory."""
import re
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def managed_root():
    root = Path(get_settings().upload_dir).resolve() / 'exports'
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise AppError('COMMON_INVALID_STATE', message='导出目录异常，删除请求需要人工核查')
    return root


def checked(path, root):
    if path.is_symlink() or path.parent != root or (path.exists() and not path.is_file()):
        raise AppError('COMMON_INVALID_STATE', message='导出文件路径异常，删除请求需要人工核查')
    return path


def owned_paths(tenant_id, export_id=None):
    root = managed_root()
    if not root.exists():
        return []
    eid = re.escape(str(export_id)) if export_id else UUID
    pattern = re.compile(rf'(?:{tenant_id}-{eid}\.zip|\.{tenant_id}-{eid}\.[0-9a-f]{{32}}\.tmp)')
    # No recursive traversal. Include orphan files whose DB transaction never committed.
    return [checked(path, root) for path in root.iterdir() if pattern.fullmatch(path.name)]


def remove_paths(paths):
    try:
        for path in paths:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise AppError('COMMON_INVALID_STATE', message='旧导出文件未清理成功，不能完成删除请求') from exc
