"""Fixed Linux worker ZIP exporter, injected with ``python3 -I -c``.

No request paths or shell commands are accepted. stdout is ZIP bytes only;
exit 0 is the completion marker. The broker must discard all bytes on failure.
Links are archived as links, never traversed. Hardlinks and special files fail
the entire export. No temporary archive is created in the workspace.
"""
import os
import re
import signal
import stat
import sys
import time
import zipfile

ROOT = "/workspace"
MAX_RAW_BYTES = 512 * 1024 * 1024
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 50000
MAX_DEPTH = 100
MAX_NAME_BYTES = 16 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_SECONDS = 180
ERROR_MESSAGES = frozenset({
    "工作区包含无法安全打包的文件名，请重命名后重试",
    "打包时文件发生变化，请停止修改后重新下载",
    "打包时目录发生变化，请重新下载",
    "工作区包含跨设备目录或文件，无法安全打包",
    "工作区包含硬链接，无法安全打包；请改为普通文件后重试",
    "工作区包含特殊文件，无法安全打包",
    "工作区目录超过 100 层，请整理后重试",
    "工作区超过 50000 个文件或目录，请整理后重试",
    "工作区文件总大小超过 512 MiB，请缩小后重试",
    "工作区文件路径信息过大，请整理后重试",
    "压缩包超过 256 MiB，请缩小工作区后重试",
    "工作区打包超时，请缩小工作区后重试",
    "下载连接已中断，压缩包未完成",
    "工作区打包失败：文件发生变化、不可读取或不符合安全要求，请整理后重试",
})


class ArchiveError(ValueError):
    pass


def safe_component(name):
    if (not isinstance(name, str) or not name or name in (".", "..")
            or any(char in name for char in '/\\:<>"|?*')
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or name.endswith((".", " "))):
        raise ArchiveError("工作区包含无法安全打包的文件名，请重命名后重试")
    try:
        encoded = name.encode("utf-8")
    except UnicodeError:
        raise ArchiveError("工作区包含无法安全打包的文件名，请重命名后重试") from None
    if len(encoded) > 255 or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name):
        raise ArchiveError("工作区包含无法安全打包的文件名，请重命名后重试")


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def unchanged(before, after):
    if stamp(before) != stamp(after):
        raise ArchiveError("打包时文件发生变化，请停止修改后重新下载")


def safe_type(info, device):
    if info.st_dev != device:
        raise ArchiveError("工作区包含跨设备目录或文件，无法安全打包")
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if info.st_nlink != 1:
        raise ArchiveError("工作区包含硬链接，无法安全打包；请改为普通文件后重试")
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISLNK(info.st_mode):
        return "link"
    raise ArchiveError("工作区包含特殊文件，无法安全打包")


class Budget:
    def __init__(self):
        self.entries = self.size = self.names = 0

    def add(self, path, size, depth):
        if depth > MAX_DEPTH:
            raise ArchiveError("工作区目录超过 100 层，请整理后重试")
        self.entries += 1
        self.size += size
        self.names += len(path.encode("utf-8"))
        if self.entries > MAX_ENTRIES:
            raise ArchiveError("工作区超过 50000 个文件或目录，请整理后重试")
        if size < 0 or self.size > MAX_RAW_BYTES:
            raise ArchiveError("工作区文件总大小超过 512 MiB，请缩小后重试")
        if self.names > MAX_NAME_BYTES:
            raise ArchiveError("工作区文件路径信息过大，请整理后重试")


class LimitedWriter:
    """Unseekable bounded output; zipfile retains only bounded entry metadata."""
    def __init__(self, output):
        self.output, self.size = output, 0

    def tell(self):
        return self.size

    def write(self, data):
        length = len(data)
        if self.size + length > MAX_ZIP_BYTES:
            raise ArchiveError("压缩包超过 256 MiB，请缩小工作区后重试")
        view = memoryview(data)
        while view:
            count = self.output.write(view)
            if not isinstance(count, int) or not 0 < count <= len(view):
                raise OSError("Archive output interrupted")
            self.size += count
            view = view[count:]
        return length

    def flush(self):
        self.output.flush()


def zip_info(path, info, kind, size=0):
    timestamp = time.gmtime(max(315532800, min(info.st_mtime, 4354819198)))[:6]
    entry = zipfile.ZipInfo(path + ("/" if kind == "directory" else ""), timestamp)
    entry.create_system = 3
    entry.compress_type = zipfile.ZIP_DEFLATED
    mode = (stat.S_IFDIR if kind == "directory" else stat.S_IFLNK if kind == "link" else stat.S_IFREG)
    entry.external_attr = (mode | (stat.S_IMODE(info.st_mode) & 0o777)) << 16
    if kind == "directory":
        entry.external_attr |= 0x10
    entry.file_size = size
    return entry


def open_child(parent, name, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    return os.open(name, flags, dir_fd=parent)


def walk(parent, prefix, depth, device, archive, records, budget, verify=False):
    directory_before = os.fstat(parent)
    with os.scandir(parent) as entries:
        for item in entries:
            safe_component(item.name)
            path = prefix + item.name
            before = os.stat(item.name, dir_fd=parent, follow_symlinks=False)
            kind = safe_type(before, device)
            target = os.readlink(item.name, dir_fd=parent) if kind == "link" else None
            data = os.fsencode(target) if target is not None else None
            size = len(data) if data is not None else before.st_size if kind == "file" else 0
            budget.add(path, size, depth)
            value = (stamp(before), data)
            if verify:
                if records.get(path) != value:
                    raise ArchiveError("打包时文件发生变化，请停止修改后重新下载")
            else:
                if path in records:
                    raise ArchiveError("打包时目录发生变化，请重新下载")
                records[path] = value
            if kind == "directory":
                child = open_child(parent, item.name, directory=True)
                try:
                    unchanged(before, os.fstat(child))
                    if not verify:
                        archive.writestr(zip_info(path, before, kind), b"")
                    walk(child, path + "/", depth + 1, device, archive, records, budget, verify)
                finally:
                    os.close(child)
            elif kind == "link":
                # Preserve only the link text and Unix type bits, including
                # dangling/absolute links. Never open or recurse into its target.
                if not verify:
                    archive.writestr(zip_info(path, before, kind, size), data)
            elif not verify:
                child = open_child(parent, item.name)
                try:
                    opened = os.fstat(child)
                    safe_type(opened, device)
                    unchanged(before, opened)
                    remaining = size
                    with archive.open(zip_info(path, before, kind, size), "w") as output:
                        while True:
                            block = os.read(child, min(CHUNK_SIZE, remaining + 1))
                            if len(block) > remaining:
                                raise ArchiveError("打包时文件发生变化，请停止修改后重新下载")
                            if not block:
                                if remaining:
                                    raise ArchiveError("打包时文件发生变化，请停止修改后重新下载")
                                break
                            output.write(block)
                            remaining -= len(block)
                    unchanged(before, os.fstat(child))
                finally:
                    os.close(child)
            unchanged(before, os.stat(item.name, dir_fd=parent, follow_symlinks=False))
    unchanged(directory_before, os.fstat(parent))


def export(output, root_path=ROOT):
    root = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(root)
        records, budget = {}, Budget()
        writer = LimitedWriter(output)
        with zipfile.ZipFile(writer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3,
                             allowZip64=False) as archive:
            walk(root, "", 1, before.st_dev, archive, records, budget)
        # Detect replaced/deleted/added entries, including already-read files.
        # This is a consistency check, not a filesystem snapshot or write lock.
        verified = Budget()
        walk(root, "", 1, before.st_dev, None, records, verified, verify=True)
        if verified.entries != len(records):
            raise ArchiveError("打包时文件发生变化，请停止修改后重新下载")
        unchanged(before, os.fstat(root))
        unchanged(before, os.stat(root_path, follow_symlinks=False))
        writer.flush()
    finally:
        os.close(root)


def timed_out(_signum, _frame):
    raise ArchiveError("工作区打包超时，请缩小工作区后重试")


def main():
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(MAX_SECONDS)
    try:
        export(sys.stdout.buffer)
        return 0
    except BrokenPipeError:
        # Avoid an interpreter-shutdown flush retry on a disconnected pipe.
        try:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, sys.stdout.fileno())
            finally:
                os.close(null)
        except (OSError, AttributeError):
            pass
        print("下载连接已中断，压缩包未完成", file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc) if isinstance(exc, ArchiveError) else
              "工作区打包失败：文件发生变化、不可读取或不符合安全要求，请整理后重试", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
