"""Fixed worker-side file protocol. Linux dir_fd/O_NOFOLLOW prevents path races.

Invoked through docker exec as the unprivileged workspace user, never as root.
The broker does not mount any user-controlled files, volumes, or credentials.
"""
import base64
import binascii
import json
import os
import re
import secrets
import stat
import sys

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_ENCODED_SIZE = ((MAX_FILE_SIZE + 2) // 3) * 4
MAX_REQUEST_SIZE = 16 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_DELETE_ENTRIES = 10000
ROOT = "/workspace"


def parts(path: str, allow_root: bool = False) -> list[str]:
    if not isinstance(path, str) or len(path) > 1024 or "\\" in path or "\x00" in path:
        raise ValueError("无效的相对路径")
    if path == "" and allow_root:
        return []
    items = path.split("/")
    if any(p in ("", ".", "..") or ":" in p or len(p.encode()) > 255 for p in items):
        raise ValueError("只允许工作区内的相对路径")
    return items


def directory(root: int, items: list[str]) -> int:
    fd = os.dup(root)
    try:
        for item in items:
            child = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def upload_name(name):
    if (not isinstance(name, str) or len(parts(name)) != 1
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("文件名必须是不含路径的普通名称")
    return name


def attachment_parts(path):
    items = parts(path)
    if (len(items) != 3 or items[0] != "uploads"
            or not re.fullmatch(r"[a-f0-9]{32}", items[1])):
        raise ValueError("请选择当前工作区中已上传的附件")
    upload_name(items[2])
    return items


def media_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def decode_content(content, encoding):
    if not isinstance(content, str) or encoding not in ("utf-8", "base64"):
        raise ValueError("无效的文件内容")
    if len(content) > MAX_ENCODED_SIZE:
        raise ValueError("文件超过 10 MiB")
    try:
        data = content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("无效的文件编码")
    if len(data) > MAX_FILE_SIZE:
        raise ValueError("文件超过 10 MiB")
    return data


def upload(root, request):
    name = upload_name(request.get("name"))
    data = decode_content(request.get("content"), "base64")
    try:
        os.mkdir("uploads", mode=0o700, dir_fd=root)
    except FileExistsError:
        pass
    uploads = directory(root, ["uploads"])
    folder = None
    identifier = secrets.token_hex(16)
    try:
        # O_EXCL-style directory creation and a server-generated ID ensure an
        # upload can never overwrite an existing project or another attachment.
        os.mkdir(identifier, mode=0o700, dir_fd=uploads)
        folder = directory(uploads, [identifier])
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=folder)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            os.unlink(name, dir_fd=folder)
            raise
        return {"path": f"uploads/{identifier}/{name}", "name": name,
                "size": len(data), "mediaType": media_type(data)}
    finally:
        if folder is not None:
            os.close(folder)
        os.close(uploads)


def check_regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("不支持符号链接、硬链接或特殊文件")


def remove_entry(parent: int, name: str, budget: list[int]):
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("目录过大，请在 Claude 中分批删除")
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for entry in os.listdir(child):
                remove_entry(child, entry, budget)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=parent)
    else:
        # Unlink the directory entry itself, never follow it.
        os.unlink(name, dir_fd=parent)


def execute(request: dict, root_path: str = ROOT):
    if not isinstance(request, dict):
        raise ValueError("无效的文件请求")
    op = request.get("op")
    path = request.get("path", "")
    items = parts(path, allow_root=op in ("list", "upload"))
    if op == "attachment":
        attachment_parts(path)
    root = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent = None
    try:
        if op == "upload":
            if path:
                raise ValueError("上传不接受目标路径")
            return upload(root, request)
        if op == "list":
            parent = directory(root, items)
            entries = []
            with os.scandir(parent) as scan:
                for entry in scan:
                    if len(entries) >= MAX_ENTRIES:
                        raise ValueError("目录超过 1000 项，请在 Claude 中整理后重试")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else "blocked"
                    entries.append({"name": entry.name, "type": kind, "size": info.st_size})
            return {"path": path, "entries": sorted(entries, key=lambda e: (e["type"] != "directory", e["name"].lower()))}
        parent = directory(root, items[:-1])
        name = items[-1]
        if op in ("read", "attachment"):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                check_regular(info)
                if info.st_size > MAX_FILE_SIZE:
                    raise ValueError("文件超过 10 MiB")
                with os.fdopen(fd, "rb", closefd=False) as file:
                    if op == "attachment":
                        return {"path": path, "name": name, "size": info.st_size,
                                "mediaType": media_type(file.read(16))}
                    data = file.read(MAX_FILE_SIZE + 1)
                if len(data) > MAX_FILE_SIZE:
                    raise ValueError("文件超过 10 MiB")
            finally:
                os.close(fd)
            try:
                content = data.decode("utf-8")
                encoding = "utf-8" if "\x00" not in content else "base64"
                if len(json.dumps(content, ensure_ascii=False).encode()) > MAX_ENCODED_SIZE:
                    encoding = "base64"
            except UnicodeDecodeError:
                encoding = "base64"
            if encoding == "base64":
                content = base64.b64encode(data).decode("ascii")
            return {"path": path, "content": content, "encoding": encoding, "size": len(data),
                    "mediaType": media_type(data)}
        if op == "write":
            data = decode_content(request.get("content", ""), request.get("encoding", "utf-8"))
            mode = 0o600
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
                check_regular(existing)
                mode = stat.S_IMODE(existing.st_mode) & 0o777
            except FileNotFoundError:
                pass
            temporary = ".cc-save-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    os.fchmod(file.fileno(), mode)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            return {"ok": True}
        if op == "mkdir":
            os.mkdir(name, mode=0o700, dir_fd=parent)
            return {"ok": True}
        if op == "delete":
            remove_entry(parent, name, [MAX_DELETE_ENTRIES])
            return {"ok": True}
        raise ValueError("不支持的文件操作")
    finally:
        if parent is not None:
            os.close(parent)
        os.close(root)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_SIZE + 1)
        if len(raw) > MAX_REQUEST_SIZE:
            raise ValueError("请求过大")
        result = execute(json.loads(raw))
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "文件不存在、被占用、无权限或属于不允许访问的链接"
        print(json.dumps({"error": message}, ensure_ascii=True))
        sys.exit(1)


if __name__ == "__main__":
    main()
