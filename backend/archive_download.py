"""Bounded, private workspace exports; ZIP contents never enter JSON or RAM wholesale."""
import asyncio
from pathlib import Path
import tempfile
from urllib.parse import quote
import zipfile

from fastapi import HTTPException
from starlette.responses import StreamingResponse

CHUNK_SIZE = 64 * 1024
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_RAW_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 50000
BUILD_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 240


async def prepare_connected(request, prepare):
    """Abort expensive preparation when the requesting browser goes away."""
    async def disconnected():
        while not await request.is_disconnected():
            await asyncio.sleep(0.25)

    build = asyncio.create_task(prepare())
    watcher = asyncio.create_task(disconnected())
    transferred = False
    try:
        done, _ = await asyncio.wait((build, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise HTTPException(499, "下载连接已中断")
        artifact = await build
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        transferred = True
        return artifact
    finally:
        if not transferred:
            watcher.cancel()
            if not build.done():
                build.cancel()
            results = await asyncio.gather(build, watcher, return_exceptions=True)
            if not isinstance(results[0], BaseException):
                results[0].close()


class ArchiveArtifact:
    def __init__(self, file, release):
        self.file, self.release = file, release
        self.size = 0
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.file.close()
        finally:
            self.release()


class ArchiveExports:
    def __init__(self, broker):
        self.broker = broker
        # Includes both generation and transmission: a slow download must not
        # permit accumulating many private temporary archives on disk.
        self.slots = asyncio.Semaphore(1)
        self.active = None

    async def prepare(self, workspace_id, authorize):
        from .docker_backend import BrokerError
        authorize()
        if self.slots.locked():
            raise BrokerError("服务器正在打包或下载其他工作区，请稍后重试", 429)
        await self.slots.acquire()
        artifact = None
        complete = False
        try:
            # DATA_DIR is a private disk-backed volume, unlike the web service's
            # small /tmp tmpfs. Linux uses an anonymous, automatically removed file.
            artifact = ArchiveArtifact(tempfile.TemporaryFile(mode="w+b", prefix=".workspace-export-",
                                                              dir=self.broker.config.data_dir), self.slots.release)
            self.active = artifact
            async with asyncio.timeout(BUILD_TIMEOUT):
                workspace = await self.broker.workspace(workspace_id)
                authorize()
                if not workspace["running"]:
                    raise BrokerError("请先启动工作区，再下载 ZIP", 409)
                async with self.broker.commands:
                    authorize()
                    await self.capture(workspace_id, artifact, authorize)
                authorize()
                artifact.file.seek(0)
                try:
                    with zipfile.ZipFile(artifact.file) as archive:
                        entries = archive.infolist()
                        if len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_RAW_BYTES:
                            raise BrokerError("工作区超过打包限制：最多 512 MiB 原文件、50000 项", 413)
                except (zipfile.BadZipFile, OSError, ValueError):
                    raise BrokerError("打包未生成完整 ZIP，请稍后重试", 503) from None
                artifact.file.seek(0)
                complete = True
                return artifact
        except TimeoutError:
            raise BrokerError("打包超过 180 秒，请结束写文件的任务或减少文件后重试", 504) from None
        except OSError:
            raise BrokerError("无法生成压缩包，请检查服务器剩余空间后重试", 503) from None
        finally:
            # Success transfers ownership to ArchiveResponse, including the
            # semaphore lease; every other exit removes the temporary archive.
            if not complete:
                if artifact is not None:
                    artifact.close()
                else:
                    self.slots.release()

    async def capture(self, workspace_id, artifact, authorize):
        from .docker_backend import BrokerError
        helper = Path(__file__).with_name("archive_helper.py").read_text(encoding="utf-8")
        process = None
        stderr_task = None
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "exec", "--user", "1000:1000", self.broker.names(workspace_id)["container"],
                "/usr/bin/timeout", "--signal=TERM", "--kill-after=5s", "180s",
                "/usr/bin/python3", "-I", "-c", helper,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=CHUNK_SIZE)
            async def read_stderr():
                result = bytearray()
                while chunk := await process.stderr.read(CHUNK_SIZE):
                    result.extend(chunk[:max(0, 4096 - len(result))])
                return bytes(result)
            stderr_task = asyncio.create_task(read_stderr())
            while True:
                authorize()
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(CHUNK_SIZE), timeout=1)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                authorize()
                artifact.size += len(chunk)
                if artifact.size > MAX_ZIP_BYTES:
                    raise BrokerError("ZIP 超过 256 MiB，请减少文件后重试", 413)
                artifact.file.write(chunk)
            code = await process.wait()
            error = (await stderr_task).decode("utf-8", errors="replace").strip()
            authorize()
            if code:
                if code in (124, 137):
                    raise BrokerError("打包超过 180 秒，请结束写文件的任务或减少文件后重试", 504)
                # Worker/Docker diagnostics must never reflect arbitrary paths,
                # credential text, or other server details to the browser.
                from .archive_helper import ERROR_MESSAGES
                if error in ERROR_MESSAGES:
                    raise BrokerError(error, 400)
                raise BrokerError("工作区打包失败，请检查工作区状态后重试", 503)
        finally:
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                # Drain the bounded pipe buffers after killing the CLI, so a
                # paused stdout reader cannot deadlock Process.wait().
                try:
                    await asyncio.wait_for(process.communicate(), timeout=5)
                except TimeoutError:
                    pass

    def close(self):
        if self.active:
            self.active.close()


class ArchiveResponse(StreamingResponse):
    def __init__(self, artifact, name, workspace_id, authorize):
        self.artifact = artifact
        self.authorize = authorize
        fallback = f"workspace-{workspace_id[:8]}.zip"
        filename = quote(f"{name}.zip", safe="")
        super().__init__(self.chunks(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{filename}',
            "Content-Length": str(artifact.size), "Cache-Control": "no-store",
        })

    async def chunks(self):
        try:
            while True:
                self.authorize()
                chunk = self.artifact.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
                await asyncio.sleep(0)
        finally:
            self.artifact.close()

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                return await super().__call__(scope, receive, send)
        finally:
            # Also covers disconnect before first chunk, or an interrupted send.
            self.artifact.close()
