from __future__ import annotations

import html
from pathlib import Path
from typing import Protocol

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from qrcode.constants import ERROR_CORRECT_M

from .contracts import CommandError, RemoteCommand, RemotePrototypeError
from .protocol import EncryptedCommandSession, encode_key

_ASSETS = Path(__file__).with_name("assets")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class RemoteController(Protocol):
    async def apply(self, command: RemoteCommand) -> None: ...


def _asset(name: str) -> bytes:
    return (_ASSETS / name).read_bytes()


def _templated_asset(name: str, *, session_id: str) -> str:
    template = (_ASSETS / name).read_text(encoding="utf-8")
    return template.replace("__SESSION_ID__", html.escape(session_id, quote=True))


def _secure_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"


def create_remote_app(
    *,
    session_id: str,
    pairing_key: bytes,
    public_base_url: str,
    controller: RemoteController,
    command_session: EncryptedCommandSession | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    encrypted_session = command_session or EncryptedCommandSession(
        key=pairing_key,
        session_id=session_id,
    )
    pair_url = f"{public_base_url.rstrip('/')}/pair/{session_id}#{encode_key(pairing_key)}"

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        _secure_headers(response)
        return response

    @app.get(f"/host/{session_id}", response_class=HTMLResponse)
    async def host_page() -> str:
        return _templated_asset("host.html", session_id=session_id)

    @app.get(f"/host/{session_id}/qr.svg")
    async def qr_code(request: Request) -> Response:
        client_host = request.client.host if request.client is not None else ""
        if client_host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=404, detail="not found")
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=3)
        qr.add_data(pair_url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        return Response(image.to_string(), media_type="image/svg+xml")

    @app.get(f"/pair/{session_id}", response_class=HTMLResponse)
    async def phone_page() -> str:
        return _templated_asset("phone.html", session_id=session_id)

    @app.get("/assets/tweetnacl.min.js")
    async def tweetnacl() -> Response:
        return Response(_asset("tweetnacl.min.js"), media_type="text/javascript")

    @app.get("/assets/phone.js")
    async def phone_script() -> Response:
        return Response(_asset("phone.js"), media_type="text/javascript")

    @app.post(f"/api/sessions/{session_id}/commands")
    async def command(request: Request) -> dict[str, object]:
        try:
            frame = await request.json()
            if not isinstance(frame, dict):
                raise CommandError("invalid encrypted command")
            parsed = encrypted_session.decrypt(frame)
            await controller.apply(parsed)
        except (CommandError, RemotePrototypeError, ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid encrypted command") from None
        status = "paired" if encrypted_session.paired else "waiting"
        return {"ok": True, "status": status}

    return app
