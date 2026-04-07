import asyncio
import base64
import logging
import os
import shutil
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, List

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from app.model import BACKEND_ROOT, run_triposr

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="VAREM Backend")

# CORS: "*" with allow_credentials=False (browser-compatible). For cookies, set real origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_errors(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    details = exc.errors()
    logger.warning("POST %s validation failed: %s", request.url.path, details)
    print("[api] validation error:", details)
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_failed",
            "detail": details,
            "hint": (
                "Use multipart field 'file' (one) or 'images' (one or more) "
                "on /save and /generate."
            ),
        },
    )


def _resolve_model_path(model_path: str) -> str:
    p = Path(model_path)
    if not p.is_absolute():
        p = BACKEND_ROOT / p
    return str(p.resolve())


def _collect_uploads(
    file: UploadFile | None,
    images: List[UploadFile] | None,
) -> List[UploadFile] | None:
    if file is not None:
        return [file]
    if images:
        return list(images)
    return None


def _ensure_temp_output() -> tuple[Path, Path] | JSONResponse:
    """Your simple layout: ``Backend/temp`` and ``Backend/output``."""
    temp_dir = BACKEND_ROOT / "temp"
    out_dir = BACKEND_ROOT / "output"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={
                "error": "Could not create temp/output folders",
                "detail": str(e),
                "hint": "If 'temp' or 'output' are files, delete them or use folders only.",
            },
        )
    return temp_dir, out_dir


async def _save_uploads_to_temp(
    uploads: List[UploadFile],
    temp_dir: Path,
) -> tuple[list[dict[str, Any]], JSONResponse | None]:
    infos: list[dict[str, Any]] = []
    image_paths: list[str] = []

    for img in uploads:
        suffix = Path(img.filename or "upload").suffix or ".png"
        safe_name = f"{uuid.uuid4().hex}{suffix}"
        path = temp_dir / safe_name
        stream = img.file
        if stream is None:
            return [], JSONResponse(
                status_code=400,
                content={"error": "Upload stream was missing"},
            )
        try:
            with path.open("wb") as buffer:
                await asyncio.to_thread(shutil.copyfileobj, stream, buffer)
        except OSError as e:
            logger.exception("Failed to save %s: %s", img.filename, e)
            return [], JSONResponse(
                status_code=500,
                content={"error": "Failed to save upload", "detail": str(e)},
            )
        abs_path = str(path.resolve())
        image_paths.append(abs_path)
        infos.append(
            {
                "original_name": img.filename,
                "stored_as": safe_name,
                "path": abs_path,
            },
        )
        print(f"[temp] saved {img.filename!r} -> {abs_path}")

    print("Saved images:", image_paths)
    return infos, None


def _list_temp_files() -> list[dict[str, Any]]:
    temp_dir = BACKEND_ROOT / "temp"
    if not temp_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(temp_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        st = p.stat()
        out.append(
            {
                "filename": p.name,
                "path": str(p.resolve()),
                "size_bytes": st.st_size,
                "modified_utc": datetime.fromtimestamp(
                    st.st_mtime, tz=timezone.utc
                ).isoformat(),
            },
        )
    return out


def _print_confirmation(title: str, message: str, infos: list[dict[str, Any]]) -> None:
    line = "=" * 60
    print(line)
    print(f"  {title}")
    print(f"  {message}")
    print(f"  temp folder: {BACKEND_ROOT / 'temp'}")
    for i, item in enumerate(infos, 1):
        print(
            f"    {i}. {item.get('stored_as')} "
            f"(from {item.get('original_name')!r})",
        )
    print(line)


# --- Routes ---


@app.get("/")
def home():
    """Simple status (your snippet) + discovery links (previous API)."""
    return {
        "message": "API is working",
        "docs": "/docs",
        "health": "/health",
        "list_saved_in_temp": "/api/uploads",
        "save": "POST /save",
        "generate": "POST /generate",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/uploads")
@app.get("/uploads/list")
def list_saved_in_temp():
    """Files under ``Backend/temp/`` (same uploads ``/generate`` uses)."""
    files = _list_temp_files()
    return {
        "ok": True,
        "count": len(files),
        "directory": str((BACKEND_ROOT / "temp").resolve()),
        "files": files,
        "message": f"{len(files)} file(s) in temp.",
    }


@app.post("/save", response_model=None)
@app.post("/api/save", response_model=None)
async def save_images(
    file: Annotated[UploadFile | None, File()] = None,
    images: Annotated[List[UploadFile] | None, File()] = None,
) -> JSONResponse:
    uploads = _collect_uploads(file, images)
    if not uploads:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "No images uploaded",
                "hint": "Use field 'file' or 'images'.",
                "count": 0,
            },
        )

    ensured = _ensure_temp_output()
    if isinstance(ensured, JSONResponse):
        return ensured
    temp_dir, _out = ensured

    infos, err = await _save_uploads_to_temp(uploads, temp_dir)
    if err is not None:
        return err

    count = len(infos)
    msg = f"Saved {count} image(s)."
    payload = {
        "ok": True,
        "count": count,
        "message": msg,
        "detail": msg,
        "files": infos,
        "directory": str(temp_dir.resolve()),
    }
    logger.info("%s", msg)
    _print_confirmation("SAVE COMPLETE", msg, infos)
    return JSONResponse(status_code=200, content=payload)


@app.post("/generate", response_model=None)
@app.post("/api/generate", response_model=None)
async def generate(
    file: Annotated[UploadFile | None, File()] = None,
    images: Annotated[List[UploadFile] | None, File()] = None,
    response_format: Annotated[str, Query()] = "json",
) -> Response:
    """
    Combined behavior:
    - Saves all uploads under ``Backend/temp/`` (your simple flow).
    - Calls ``run_triposr`` on the first saved image (stub → ``sample.glb``).
    - Default ``response_format=json`` returns JSON + ``glb_base64`` for frontends
      that use ``response.json()``; use ``?response_format=file`` for raw GLB.
    """
    uploads = _collect_uploads(file, images)
    if not uploads:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "No images uploaded",
                "hint": "Use field 'file' or 'images' (same as /save).",
            },
        )

    ensured = _ensure_temp_output()
    if isinstance(ensured, JSONResponse):
        return ensured
    temp_dir, _out_dir = ensured

    try:
        infos, err = await _save_uploads_to_temp(uploads, temp_dir)
        if err is not None:
            return err
        count = len(infos)
        first_path = infos[0]["path"]

        try:
            model_path = await asyncio.to_thread(run_triposr, first_path)
        except Exception as e:
            logger.exception("run_triposr failed: %s", e)
            traceback.print_exc()
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": "Model generation failed",
                    "detail": str(e),
                    "images_saved": count,
                    "files": infos,
                },
            )

        resolved = _resolve_model_path(model_path)
        if not Path(resolved).is_file():
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Model file not found",
                    "expected": resolved,
                    "images_saved": count,
                    "files": infos,
                },
            )

        print("Returning model:", resolved)

        want_json = (response_format or "json").lower() not in (
            "file",
            "binary",
            "glb",
        )

        if want_json:
            raw = Path(resolved).read_bytes()
            b64 = base64.b64encode(raw).decode("ascii")
            gen_msg = f"Saved {count} image(s) and generated model."
            body = {
                "ok": True,
                "images_saved": count,
                "message": gen_msg,
                "detail": gen_msg,
                "files": infos,
                "directory": str(temp_dir.resolve()),
                "glb_filename": os.path.basename(resolved),
                "glb_path": resolved,
                "glb_media_type": "model/gltf-binary",
                "glb_base64": b64,
            }
            _print_confirmation("GENERATE COMPLETE", gen_msg, infos)
            logger.info("%s", gen_msg)
            return JSONResponse(status_code=200, content=body)

        _print_confirmation(
            "GENERATE COMPLETE",
            f"Saved {count} image(s); returning GLB file.",
            infos,
        )
        resp = FileResponse(
            resolved,
            media_type="model/gltf-binary",
            filename=os.path.basename(resolved),
        )
        resp.headers["X-Images-Saved"] = str(count)
        return resp

    except Exception as e:
        logger.exception("generate: %s", e)
        print("ERROR:", str(e))
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})
