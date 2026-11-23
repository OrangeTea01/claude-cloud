"""Internal-only translation service; no external calls during request handling."""
import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import LocalTranslator, MAX_TEXT


class TranslateBody(BaseModel):
    text: str = Field(max_length=MAX_TEXT)
    source: Literal["zh", "en"]
    target: Literal["zh", "en"]


def create_app(engine=None):
    engine = engine or LocalTranslator()
    slot = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(engine.warm)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def bound_body(request: Request, call_next):
        if request.method == "POST":
            size = 0
            content = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 512000:
                    return JSONResponse({"detail": "Translation request too large"}, status_code=413)
                content.append(chunk)
            request._body = b"".join(content)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid translation request"}, status_code=422)

    @app.get("/health")
    async def health():
        payload = {"ready": engine.ready, "engine": "argos-ctranslate2-cpu", "languages": ["zh", "en"]}
        return JSONResponse(payload, status_code=200 if engine.ready else 503)

    @app.post("/translate")
    async def translate(body: TranslateBody):
        if body.source == body.target:
            raise HTTPException(422, "Translation direction must be zh↔en")
        if not engine.ready:
            raise HTTPException(503, "Local translation models are not ready")
        try:
            await asyncio.wait_for(slot.acquire(), timeout=5)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Local translator is busy; retry shortly")
        try:
            task = asyncio.create_task(asyncio.to_thread(engine.translate, body.text, body.source, body.target))
            try:
                output = await asyncio.shield(task)
            except asyncio.CancelledError:
                # CPU inference cannot be canceled mid-call. Hold the slot until
                # it finishes so a disconnected client cannot spawn extra work.
                await task
                raise
            return {"text": output}
        except ValueError:
            raise HTTPException(422, "Translation text exceeds a safe processing limit")
        except Exception:
            # Never log/reflect user's message or model input from exceptions.
            raise HTTPException(503, "Local translation failed; no untranslated message was sent")
        finally:
            slot.release()

    return app


app = create_app()
