"""Browse and delete files inside the download directory."""
import os
import shutil
from pathlib import Path

import config

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".webm"}


class FileError(Exception):
    pass


def _resolve(rel_path):
    """Map a relative path to an absolute one, refusing anything outside the download directory."""
    root = config.DOWNLOAD_DIR.resolve()
    target = (root / (rel_path or "").lstrip("/\\")).resolve()
    if target != root and root not in target.parents:
        raise FileError("Path outside of the download directory")
    return root, target


def _dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def list_directory(rel_path=""):
    root, target = _resolve(rel_path)
    if not target.is_dir():
        return {"path": "", "items": []}

    items = []
    for entry in os.scandir(target):
        try:
            is_dir = entry.is_dir()
            stat = entry.stat()
        except OSError:
            continue
        if is_dir:
            kind = "folder"
        elif Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS:
            kind = "video"
        elif entry.name.endswith(".part"):
            kind = "partial"
        else:
            kind = "file"
        items.append({
            "name": entry.name,
            "type": kind,
            "size": _dir_size(entry.path) if is_dir else stat.st_size,
            "modified": stat.st_mtime,
            "path": Path(entry.path).relative_to(root).as_posix(),
        })
    return {"path": target.relative_to(root).as_posix() if target != root else "", "items": items}


def delete_items(paths):
    deleted = 0
    for rel_path in paths:
        try:
            root, target = _resolve(rel_path)
        except FileError:
            continue
        if target == root:
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                continue
            deleted += 1
        except OSError:
            pass
    return {"deleted": deleted}


def storage():
    path = config.DOWNLOAD_DIR if config.DOWNLOAD_DIR.exists() else Path("/")
    total, used, free = shutil.disk_usage(path)
    return {"total": total, "used": used, "free": free, "percent": round(used * 100 / total) if total else 0}
