"""Apply browser CORS policy outside the complete application error stack."""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from app.config import settings
from app.middleware.csrf import BrowserOriginMiddleware


class CorsFastAPI(FastAPI):
    """Keep FastAPI's API while including generated 500 responses in CORS.

    add_middleware puts CORS inside ServerErrorMiddleware. Wrapping the built
    stack also covers unhandled errors, without changing router, lifespan,
    dependency overrides or the existing authentication/retry middleware.
    """

    def build_middleware_stack(self) -> ASGIApp:
        return CORSMiddleware(
            BrowserOriginMiddleware(super().build_middleware_stack()),
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
