#!/usr/bin/env python3
import ipaddress
import io
import os
import threading
import time
from typing import Optional, List, Tuple

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from PIL import Image, ImageSequence

# -----------------------------------------------------------------------------
# Paths & runtime dir
# -----------------------------------------------------------------------------
RUN_DIR = os.environ.get("LED_RUNTIME_DIR") or os.environ.get("LED_RUN_DIR", "/run/ledmatrix")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "0"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "0"))
os.makedirs(RUN_DIR, exist_ok=True)

PATH_LAST = os.path.join(RUN_DIR, "last_payload.bin")
PATH_CURR = os.path.join(RUN_DIR, "ledmatrix_current.gif")
CURRENT_GIF = PATH_CURR  # canonical path used by the player

# Optional IP allowlist (comma-separated CIDR list)
def _parse_allow_nets(raw: str):
    if not raw:
        return None
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            print(f"Invalid ALLOW_NETS entry: {part}")
    return nets or None

ALLOW_NETS = _parse_allow_nets(os.environ.get("ALLOW_NETS", ""))

# Event to interrupt the player loop when a new file arrives
CHANGE_EVENT = threading.Event()

# -----------------------------------------------------------------------------
# Matrix setup (lazy)
# -----------------------------------------------------------------------------
_MATRIX = None

def get_matrix():
    global _MATRIX
    if _MATRIX is not None:
        return _MATRIX
    try:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions
        opts = RGBMatrixOptions()
        opts.hardware_mapping = os.environ.get("LED_HARDWARE_MAPPING", "regular")
        opts.rows = int(os.environ.get("LED_ROWS", "64"))
        opts.cols = int(os.environ.get("LED_COLS", "64"))
        br = int(os.environ.get("LED_BRIGHTNESS", "70"))
        br = 1 if br < 1 else (100 if br > 100 else br)
        opts.brightness = br
        if os.environ.get("LED_NO_HARDWARE_PULSE", "0") in ("1", "true", "True"):
            opts.disable_hardware_pulsing = True
        _MATRIX = RGBMatrix(options=opts)
        return _MATRIX
    except Exception as e:
        print("Matrix init failed:", repr(e))
        _MATRIX = None
        return None

def set_brightness(value: int):
    m = get_matrix()
    if not m:
        return
    try:
        m.brightness = int(value)
    except Exception:
        try:
            m.SetBrightness(int(value))
        except Exception:
            pass

def clear_matrix():
    m = get_matrix()
    if m:
        try:
            m.Clear()
        except Exception:
            pass

# -----------------------------------------------------------------------------
# GIF utils
# -----------------------------------------------------------------------------
def decode_gif_frames(data: bytes) -> Tuple[List[Image.Image], List[int]]:
    """Return RGB frames + per-frame durations(ms). Raises if not a valid GIF."""
    if not data or not data.startswith(b"GIF8"):
        raise ValueError("not-a-gif-header")
    # Verify (closes image), then reopen for reading
    with Image.open(io.BytesIO(data)) as im_verify:
        im_verify.verify()

    frames, durations = [], []
    with Image.open(io.BytesIO(data)) as im:
        for idx, frame in enumerate(ImageSequence.Iterator(im)):
            if MAX_FRAMES and (idx + 1) > MAX_FRAMES:
                raise ValueError("too-many-frames")
            fr = frame.convert("RGB").copy()
            dur = frame.info.get("duration", im.info.get("duration", 100))  # ms
            try:
                dur = int(dur)
            except Exception:
                dur = 100
            frames.append(fr)
            durations.append(max(dur, 1))
    if not frames:
        raise ValueError("no-frames")
    return frames, durations

def _matrix_size() -> Tuple[int, int]:
    m = get_matrix()
    mw = getattr(m, "width", None) if m else None
    mh = getattr(m, "height", None) if m else None
    if mw is None or mh is None:
        mw = int(os.environ.get("LED_COLS", "64"))
        mh = int(os.environ.get("LED_ROWS", "64"))
    return mw, mh

def _scale_frames_to_matrix(frames: List[Image.Image]) -> List[Image.Image]:
    mw, mh = _matrix_size()
    out = []
    for fr in frames:
        if fr.size != (mw, mh):
            fr = fr.resize((mw, mh), Image.NEAREST)
        out.append(fr)
    return out

def _blit_frame(fr: Image.Image):
    """Draw a PIL RGB frame to the matrix, with a safe fallback."""
    m = get_matrix()
    if not m:
        return
    try:
        # Most modern builds accept a PIL.Image directly
        m.SetImage(fr, 0, 0)
    except Exception:
        # Fallback: pixel loop
        mw, mh = _matrix_size()
        px = fr.load()
        canvas = m.CreateFrameCanvas()
        for y in range(mh):
            for x in range(mw):
                r, g, b = px[x, y]
                canvas.SetPixel(x, y, int(r), int(g), int(b))
        m.SwapOnVSync(canvas)

# -----------------------------------------------------------------------------
# Player thread: loop the current GIF until a new one is uploaded
# -----------------------------------------------------------------------------
def _load_frames_for_current() -> Tuple[List[Image.Image], List[int]]:
    with open(CURRENT_GIF, "rb") as f:
        data = f.read()
    frames, durations = decode_gif_frames(data)
    frames = _scale_frames_to_matrix(frames)
    return frames, durations

def player_runner():
    """Runs forever: if CURRENT_GIF exists, loop it until CHANGE_EVENT is set."""
    while True:
        try:
            if not os.path.exists(CURRENT_GIF):
                time.sleep(0.1)
                continue

            # Load the current GIF once
            frames, durations = _load_frames_for_current()
            CHANGE_EVENT.clear()

            # Loop frames until a new upload arrives
            while not CHANGE_EVENT.is_set():
                for fr, dur_ms in zip(frames, durations):
                    if CHANGE_EVENT.is_set():
                        break
                    _blit_frame(fr)
                    # Sleep in small chunks so we can interrupt quickly
                    remaining = max(0.01, dur_ms / 1000.0)
                    end = time.time() + remaining
                    while time.time() < end:
                        if CHANGE_EVENT.is_set():
                            break
                        time.sleep(0.01)
        except Exception as e:
            print("PLAYBACK ERROR:", e)
            time.sleep(0.25)  # don't spin on errors

# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI()

def _client_allowed(host: str) -> bool:
    if not ALLOW_NETS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in ALLOW_NETS)

@app.middleware("http")
async def _allowlist_middleware(request: Request, call_next):
    if ALLOW_NETS:
        host = request.client.host if request.client else ""
        if not _client_allowed(host):
            return JSONResponse(status_code=403, content={"ok": False, "detail": "forbidden"})
    return await call_next(request)

# Start the player thread on app startup
@app.on_event("startup")
def _start_player():
    t = threading.Thread(target=player_runner, daemon=True)
    t.start()

@app.get("/ping")
def ping():
    return {"ok": True, "ping": "pong"}

@app.post("/brightness")
async def brightness(request: Request):
    try:
        body = await request.json()
        val = int(body.get("value"))
        if not (1 <= val <= 100):
            raise ValueError("out-of-range")
        set_brightness(val)
        return {"ok": True, "brightness": val}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad-brightness:{e}")

@app.post("/clear")
async def clear():
    try:
        clear_matrix()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"clear-failed:{e}")

@app.post("/upload")
async def upload(request: Request, file: Optional[UploadFile] = File(None)):
    """
    Accepts either:
      - raw bytes   (curl --data-binary @file http://host:9090/upload)
      - multipart   (curl -F "file=@/path.gif;type=image/gif" http://host:9090/upload)
    """
    try:
        if MAX_UPLOAD_BYTES:
            try:
                clen = int(request.headers.get("content-length", "0"))
            except Exception:
                clen = 0
            if clen and clen > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="upload-too-large")

        # Read request body once (file field for multipart, raw body otherwise)
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/"):
            if file is None:
                raise HTTPException(status_code=400, detail="upload-failed:no-file-field")
            data = await file.read()
        else:
            data = await request.body()

        if not data:
            raise HTTPException(status_code=400, detail="upload-failed:empty-body")
        if MAX_UPLOAD_BYTES and len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload-too-large")

        # Save raw payload for debugging
        tmp_last = f"{PATH_LAST}.tmp"
        with open(tmp_last, "wb") as f:
            f.write(data)
        os.replace(tmp_last, PATH_LAST)

        # Validate GIF & save canonical copy atomically so the player never
        # reads a half-written file
        try:
            # validate
            _frames, _durations = decode_gif_frames(data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad-image:{e}")

        tmp_curr = f"{PATH_CURR}.tmp"
        with open(tmp_curr, "wb") as f:
            f.write(data)
        os.replace(tmp_curr, PATH_CURR)

        # Tell the player to reload immediately
        CHANGE_EVENT.set()

        return {"ok": True, "bytes": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload-failed:{e}")

# Optional: root help
@app.get("/")
def root():
    return JSONResponse({
        "ok": True,
        "hint_raw": "curl --data-binary @/home/pi/test.gif http://<pi>:9090/upload",
        "hint_multipart": "curl -F 'file=@/home/pi/test.gif;type=image/gif' http://<pi>:9090/upload",
        "brightness": "curl -X POST -H 'Content-Type: application/json' -d '{\"value\":60}' http://<pi>:9090/brightness",
        "clear": "curl -X POST http://<pi>:9090/clear"
    })
