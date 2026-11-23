"""HTTP client for the private, local translation sidecar. No cloud fallback."""
import asyncio
import json
import urllib.error
import urllib.request

from .docker_backend import BrokerError


class LocalTranslator:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        # Never route local messages through a host's HTTP(S)_PROXY setting.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, path, body=None, timeout=120):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.url + path, data=payload,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("oversize")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except (OSError, ValueError, urllib.error.URLError):
            raise BrokerError("本地翻译服务不可用或翻译超时，请检查 translator 服务后重试", 503)

    async def translate(self, text: str, source: str, target: str) -> str:
        if not text or len(text) > 64000 or (source, target) not in (("zh", "en"), ("en", "zh")):
            raise BrokerError("翻译文本或语言不正确")
        result = await asyncio.to_thread(self._request, "/translate", {"text": text, "source": source, "target": target})
        translated = result.get("text")
        if not isinstance(translated, str) or not translated.strip() or len(translated) > 128000:
            raise BrokerError("本地翻译没有返回有效结果；原文未自动发送", 503)
        return translated

    async def status(self):
        try:
            result = await asyncio.to_thread(self._request, "/health", None, 3)
            return {"ready": result.get("ready") is True, "engine": result.get("engine", "CTranslate2 / Argos"),
                    "languages": result.get("languages", ["zh", "en"])}
        except BrokerError as exc:
            return {"ready": False, "engine": "CTranslate2 / Argos", "languages": ["zh", "en"], "error": str(exc)}
