#!/usr/bin/env python3
import ipaddress
import io
import os
import threading
import time
from typing import Optional, List, Tuple
from email.utils import formatdate

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Response
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from PIL import Image, ImageSequence

# -----------------------------------------------------------------------------
# Paths & runtime dir
# -----------------------------------------------------------------------------
RUN_DIR = os.environ.get("LED_RUNTIME_DIR") or os.environ.get("LED_RUN_DIR", "/run/ledmatrix")
DEFAULT_GIF_PATH = os.environ.get("DEFAULT_GIF_PATH")
if DEFAULT_GIF_PATH is None:
    DEFAULT_GIF_PATH = os.path.join(os.path.expanduser("~"), "ledmatrix_default.gif")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", "0"))
MAX_FRAMES = int(os.environ.get("MAX_FRAMES", "0"))
ALLOW_NETS_RAW = os.environ.get("ALLOW_NETS", "")
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

ALLOW_NETS = _parse_allow_nets(ALLOW_NETS_RAW)

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

def _atomic_write(path: str, data: bytes):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, path)

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

def _seed_default_gif():
    if not DEFAULT_GIF_PATH:
        return
    try:
        if os.path.exists(CURRENT_GIF) and os.path.getsize(CURRENT_GIF) > 0:
            return
    except Exception:
        pass
    if not os.path.exists(DEFAULT_GIF_PATH):
        return
    try:
        with open(DEFAULT_GIF_PATH, "rb") as f:
            data = f.read()
        if not data:
            return
        decode_gif_frames(data)
        _atomic_write(PATH_CURR, data)
        CHANGE_EVENT.set()
    except Exception as e:
        print("Default GIF load failed:", e)

def _write_default_gif(data: bytes):
    if not DEFAULT_GIF_PATH:
        raise ValueError("default-path-disabled")
    default_dir = os.path.dirname(DEFAULT_GIF_PATH)
    if default_dir:
        os.makedirs(default_dir, exist_ok=True)
    _atomic_write(DEFAULT_GIF_PATH, data)

def _file_stat_headers(path: str) -> dict:
    st = os.stat(path)
    return {
        "Cache-Control": "no-store",
        "Content-Length": str(st.st_size),
        "Last-Modified": formatdate(st.st_mtime, usegmt=True),
        "Content-Type": "image/gif",
    }

def _file_info(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"exists": False}
    try:
        st = os.stat(path)
        return {"exists": True, "bytes": st.st_size, "mtime": int(st.st_mtime)}
    except Exception:
        return {"exists": False}

def _current_brightness() -> int:
    m = get_matrix()
    if m is not None:
        try:
            return int(getattr(m, "brightness"))
        except Exception:
            pass
    try:
        return int(os.environ.get("LED_BRIGHTNESS", "70"))
    except Exception:
        return 70

UI_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>LED Matrix GIF Player</title>
    <style>
      @import url("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600&display=swap");
      :root {
        --bg-1: #0d1b2a;
        --bg-2: #f5ead7;
        --ink: #0b0f14;
        --muted: #5c6670;
        --accent: #f05d23;
        --accent-2: #3a86ff;
        --card: #fff7ea;
        --border: rgba(16, 20, 25, 0.15);
        --shadow: 0 18px 40px rgba(9, 15, 20, 0.2);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Space Grotesk", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background: radial-gradient(1200px 600px at 10% -10%, #f7c8a9 0%, transparent 60%),
                    linear-gradient(135deg, var(--bg-1), var(--bg-2));
        min-height: 100vh;
      }
      .grid {
        position: fixed;
        inset: 0;
        background-image: linear-gradient(rgba(12, 18, 26, 0.06) 1px, transparent 1px),
                          linear-gradient(90deg, rgba(12, 18, 26, 0.06) 1px, transparent 1px);
        background-size: 28px 28px;
        pointer-events: none;
      }
      main {
        max-width: 980px;
        margin: 0 auto;
        padding: 48px 20px 60px;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 28px;
        flex-wrap: wrap;
      }
      header .title {
        min-width: 220px;
      }
      nav {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      nav a {
        text-decoration: none;
        font-size: 13px;
        padding: 8px 12px;
        border-radius: 999px;
        color: var(--muted);
        border: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.6);
      }
      nav a.active {
        color: var(--ink);
        border-color: rgba(12, 18, 26, 0.6);
        background: #ffffff;
      }
      header h1 {
        margin: 0;
        font-size: clamp(28px, 4vw, 40px);
        letter-spacing: 0.5px;
      }
      header p {
        margin: 0;
        color: var(--muted);
        font-size: 14px;
      }
      .panel {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 22px;
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 20px;
        box-shadow: var(--shadow);
        animation: rise 500ms ease both;
      }
      .card:nth-child(2) { animation-delay: 80ms; }
      @keyframes rise {
        from { opacity: 0; transform: translateY(16px); }
        to { opacity: 1; transform: translateY(0); }
      }
      .preview-frame {
        position: relative;
        background: #0b0f14;
        border-radius: 14px;
        padding: 18px;
        display: grid;
        place-items: center;
        min-height: 220px;
        border: 1px solid rgba(255, 255, 255, 0.06);
      }
      .preview-frame img {
        width: min(260px, 70vw);
        height: auto;
        image-rendering: pixelated;
        border-radius: 8px;
        box-shadow: 0 0 24px rgba(255, 186, 115, 0.35);
      }
      .preview-empty {
        position: absolute;
        color: #f5ead7;
        font-size: 14px;
        letter-spacing: 0.4px;
      }
      .status {
        margin-top: 12px;
        font-size: 13px;
        color: var(--muted);
        display: flex;
        justify-content: space-between;
      }
      form {
        display: grid;
        gap: 12px;
      }
      input[type="file"] {
        padding: 12px;
        border-radius: 10px;
        border: 1px dashed var(--border);
        background: rgba(255, 255, 255, 0.7);
      }
      label {
        font-size: 13px;
        color: var(--muted);
        display: flex;
        align-items: center;
        gap: 10px;
      }
      button {
        border: none;
        border-radius: 12px;
        padding: 12px 16px;
        font-weight: 600;
        cursor: pointer;
        transition: transform 120ms ease, box-shadow 120ms ease;
      }
      button.primary {
        background: var(--accent);
        color: #fff;
        box-shadow: 0 12px 22px rgba(240, 93, 35, 0.25);
      }
      button.secondary {
        background: var(--accent-2);
        color: #fff;
        box-shadow: 0 12px 22px rgba(58, 134, 255, 0.25);
      }
      button:active {
        transform: translateY(1px);
      }
      .log {
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.6);
        font-size: 13px;
        color: var(--muted);
        min-height: 44px;
      }
    </style>
  </head>
  <body>
    <div class="grid"></div>
    <main>
      <header>
        <div class="title">
          <h1>LED Matrix GIF Player</h1>
          <p>Live preview + manual upload</p>
        </div>
        <nav>
          <a class="active" href="/ui">Preview</a>
          <a href="/setup">Setup</a>
        </nav>
      </header>
      <section class="panel">
        <div class="card">
          <div class="preview-frame">
            <img id="preview" alt="Current GIF preview">
            <div class="preview-empty" id="previewEmpty">No GIF loaded</div>
          </div>
          <div class="status">
            <span id="statusText">Checking status...</span>
            <span id="updatedAt">--</span>
          </div>
        </div>
        <div class="card">
          <form id="uploadForm">
            <input type="file" id="gifFile" accept="image/gif">
            <label>
              <input type="checkbox" id="setDefault">
              Set as default on boot
            </label>
            <button class="primary" type="submit">Upload and play</button>
          </form>
          <button class="secondary" id="setDefaultCurrent">Set current as default</button>
          <div class="log" id="logBox">Ready.</div>
        </div>
      </section>
    </main>
    <script>
      const preview = document.getElementById("preview");
      const previewEmpty = document.getElementById("previewEmpty");
      const statusText = document.getElementById("statusText");
      const updatedAt = document.getElementById("updatedAt");
      const logBox = document.getElementById("logBox");
      let lastModified = "";

      function setLog(message, ok = true) {
        logBox.textContent = message;
        logBox.style.color = ok ? "#3b4a57" : "#b42318";
      }

      async function refreshPreview() {
        try {
          const res = await fetch("/current.gif", { method: "HEAD" });
          if (!res.ok) {
            statusText.textContent = "No GIF playing";
            preview.style.display = "none";
            previewEmpty.style.display = "block";
            updatedAt.textContent = "--";
            return;
          }
          const lm = res.headers.get("Last-Modified") || "";
          if (lm && lm !== lastModified) {
            lastModified = lm;
            preview.src = `/current.gif?t=${Date.now()}`;
          }
          statusText.textContent = "Playing";
          updatedAt.textContent = lm ? `Updated ${new Date(lm).toLocaleTimeString()}` : "--";
          preview.style.display = "block";
          previewEmpty.style.display = "none";
        } catch (err) {
          statusText.textContent = "Offline";
          updatedAt.textContent = "--";
        }
      }

      document.getElementById("uploadForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const fileInput = document.getElementById("gifFile");
        const file = fileInput.files[0];
        if (!file) {
          setLog("Choose a GIF first.", false);
          return;
        }
        const setDefault = document.getElementById("setDefault").checked;
        const formData = new FormData();
        formData.append("file", file);
        setLog("Uploading...");
        try {
          const res = await fetch(`/upload?set_default=${setDefault ? "1" : "0"}`, {
            method: "POST",
            body: formData
          });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Upload failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Uploaded ${data.bytes} bytes.`);
          refreshPreview();
        } catch (err) {
          setLog("Upload failed: network error", false);
        }
      });

      document.getElementById("setDefaultCurrent").addEventListener("click", async () => {
        setLog("Setting default...");
        try {
          const res = await fetch("/default/current", { method: "POST" });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Default failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Default saved (${data.bytes} bytes).`);
        } catch (err) {
          setLog("Default failed: network error", false);
        }
      });

      refreshPreview();
      setInterval(refreshPreview, 2000);
    </script>
  </body>
</html>
"""

SETUP_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>LED Matrix Setup</title>
    <style>
      @import url("https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600&display=swap");
      :root {
        --bg-1: #132026;
        --bg-2: #f6efe0;
        --ink: #0b0f14;
        --muted: #55606a;
        --accent: #0f8b8d;
        --accent-2: #f29f05;
        --card: #fff6e5;
        --border: rgba(16, 20, 25, 0.15);
        --shadow: 0 18px 40px rgba(9, 15, 20, 0.18);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Space Grotesk", "Trebuchet MS", sans-serif;
        color: var(--ink);
        background: radial-gradient(900px 500px at 90% -10%, #ffd9b0 0%, transparent 60%),
                    linear-gradient(135deg, var(--bg-1), var(--bg-2));
        min-height: 100vh;
      }
      .grid {
        position: fixed;
        inset: 0;
        background-image: linear-gradient(rgba(12, 18, 26, 0.06) 1px, transparent 1px),
                          linear-gradient(90deg, rgba(12, 18, 26, 0.06) 1px, transparent 1px);
        background-size: 28px 28px;
        pointer-events: none;
      }
      main {
        max-width: 1100px;
        margin: 0 auto;
        padding: 46px 20px 70px;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 28px;
        flex-wrap: wrap;
      }
      header h1 {
        margin: 0;
        font-size: clamp(26px, 4vw, 38px);
      }
      header p {
        margin: 0;
        color: var(--muted);
        font-size: 14px;
      }
      nav {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      nav a {
        text-decoration: none;
        font-size: 13px;
        padding: 8px 12px;
        border-radius: 999px;
        color: var(--muted);
        border: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.65);
      }
      nav a.active {
        color: var(--ink);
        border-color: rgba(15, 25, 35, 0.6);
        background: #ffffff;
      }
      .panel {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        gap: 22px;
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 20px;
        box-shadow: var(--shadow);
        animation: rise 500ms ease both;
      }
      .card:nth-child(2) { animation-delay: 70ms; }
      .card:nth-child(3) { animation-delay: 140ms; }
      @keyframes rise {
        from { opacity: 0; transform: translateY(16px); }
        to { opacity: 1; transform: translateY(0); }
      }
      .card h2 {
        margin: 0 0 12px;
        font-size: 18px;
      }
      .control-row {
        display: grid;
        gap: 12px;
        margin-bottom: 12px;
      }
      .range-wrap {
        display: grid;
        grid-template-columns: 1fr 72px;
        gap: 10px;
        align-items: center;
      }
      input[type="range"] {
        width: 100%;
      }
      input[type="number"] {
        padding: 8px;
        border-radius: 10px;
        border: 1px solid var(--border);
      }
      input[type="file"] {
        padding: 12px;
        border-radius: 10px;
        border: 1px dashed var(--border);
        background: rgba(255, 255, 255, 0.7);
      }
      label {
        font-size: 13px;
        color: var(--muted);
      }
      button {
        border: none;
        border-radius: 12px;
        padding: 10px 14px;
        font-weight: 600;
        cursor: pointer;
        transition: transform 120ms ease, box-shadow 120ms ease;
      }
      button.primary {
        background: var(--accent);
        color: #fff;
        box-shadow: 0 12px 22px rgba(15, 139, 141, 0.25);
      }
      button.secondary {
        background: var(--accent-2);
        color: #fff;
        box-shadow: 0 12px 22px rgba(242, 159, 5, 0.2);
      }
      button.ghost {
        background: rgba(255, 255, 255, 0.8);
        border: 1px solid var(--border);
        color: var(--ink);
      }
      button:active {
        transform: translateY(1px);
      }
      .button-row {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }
      .info-grid {
        display: grid;
        gap: 10px;
        font-size: 13px;
        color: var(--muted);
        margin-bottom: 10px;
      }
      .info-row {
        display: flex;
        justify-content: space-between;
        gap: 12px;
      }
      .info-row span:last-child {
        color: var(--ink);
        text-align: right;
      }
      .log {
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.65);
        font-size: 13px;
        color: var(--muted);
        min-height: 44px;
      }
      .stack {
        display: grid;
        gap: 12px;
      }
    </style>
  </head>
  <body>
    <div class="grid"></div>
    <main>
      <header>
        <div class="title">
          <h1>Setup Console</h1>
          <p>Brightness, defaults, and system settings.</p>
        </div>
        <nav>
          <a href="/ui">Preview</a>
          <a class="active" href="/setup">Setup</a>
        </nav>
      </header>
      <section class="panel">
        <div class="card">
          <h2>Playback Controls</h2>
          <div class="control-row">
            <label for="brightnessRange">Brightness</label>
            <div class="range-wrap">
              <input type="range" id="brightnessRange" min="1" max="100" value="70">
              <input type="number" id="brightnessValue" min="1" max="100" value="70">
            </div>
            <button class="primary" id="applyBrightness">Apply brightness</button>
          </div>
          <div class="button-row">
            <button class="ghost" id="clearMatrix">Clear display</button>
            <button class="ghost" id="loadDefault">Load default now</button>
          </div>
        </div>
        <div class="card">
          <h2>Default GIF</h2>
          <div class="info-grid">
            <div class="info-row"><span>Default path</span><span id="defaultPath">--</span></div>
            <div class="info-row"><span>Default status</span><span id="defaultInfo">--</span></div>
          </div>
          <form id="uploadDefaultForm" class="stack">
            <input type="file" id="defaultFile" accept="image/gif">
            <button class="secondary" type="submit">Upload default only</button>
          </form>
          <div class="button-row" style="margin-top:12px;">
            <button class="ghost" id="setCurrentDefault">Set current as default</button>
          </div>
        </div>
        <div class="card">
          <h2>System Status</h2>
          <div class="info-grid">
            <div class="info-row"><span>Current GIF</span><span id="currentInfo">--</span></div>
            <div class="info-row"><span>Runtime dir</span><span id="runtimeDir">--</span></div>
            <div class="info-row"><span>Matrix size</span><span id="matrixSize">--</span></div>
            <div class="info-row"><span>Hardware mapping</span><span id="hardwareMapping">--</span></div>
            <div class="info-row"><span>Brightness</span><span id="brightnessInfo">--</span></div>
            <div class="info-row"><span>Allow nets</span><span id="allowNets">--</span></div>
            <div class="info-row"><span>Max upload</span><span id="maxUpload">--</span></div>
            <div class="info-row"><span>Max frames</span><span id="maxFrames">--</span></div>
          </div>
          <button class="ghost" id="refreshStatus">Refresh status</button>
          <div class="log" id="setupLog">Ready.</div>
        </div>
      </section>
    </main>
    <script>
      const logBox = document.getElementById("setupLog");
      const brightnessRange = document.getElementById("brightnessRange");
      const brightnessValue = document.getElementById("brightnessValue");

      function setLog(message, ok = true) {
        logBox.textContent = message;
        logBox.style.color = ok ? "#3b4a57" : "#b42318";
      }

      function formatBytes(bytes) {
        if (!bytes && bytes !== 0) return "--";
        const units = ["B", "KB", "MB", "GB"];
        let idx = 0;
        let value = Number(bytes);
        while (value >= 1024 && idx < units.length - 1) {
          value /= 1024;
          idx += 1;
        }
        return `${value.toFixed(value < 10 ? 1 : 0)} ${units[idx]}`;
      }

      function formatTime(ts) {
        if (!ts) return "--";
        return new Date(ts * 1000).toLocaleString();
      }

      async function loadStatus() {
        try {
          const res = await fetch("/status");
          const data = await res.json();
          if (!res.ok) {
            setLog(`Status failed: ${data.detail || res.status}`, false);
            return;
          }
          const current = data.current || {};
          const def = data.default || {};
          const cfg = data.config || {};

          document.getElementById("currentInfo").textContent = current.exists
            ? `${formatBytes(current.bytes)} / ${formatTime(current.mtime)}`
            : "Not set";

          document.getElementById("defaultPath").textContent = def.path || "Disabled";
          document.getElementById("defaultInfo").textContent = def.exists
            ? `${formatBytes(def.bytes)} / ${formatTime(def.mtime)}`
            : "Missing";

          document.getElementById("runtimeDir").textContent = cfg.runtime_dir || "--";
          document.getElementById("matrixSize").textContent =
            cfg.cols && cfg.rows ? `${cfg.cols} x ${cfg.rows}` : "--";
          document.getElementById("hardwareMapping").textContent = cfg.hardware_mapping || "--";
          document.getElementById("brightnessInfo").textContent = cfg.brightness || "--";
          document.getElementById("allowNets").textContent = cfg.allow_nets || "none";
          document.getElementById("maxUpload").textContent = cfg.max_upload_bytes
            ? formatBytes(cfg.max_upload_bytes)
            : "No limit";
          document.getElementById("maxFrames").textContent = cfg.max_frames || "No limit";

          if (cfg.brightness) {
            brightnessRange.value = cfg.brightness;
            brightnessValue.value = cfg.brightness;
          }
        } catch (err) {
          setLog("Status failed: network error", false);
        }
      }

      brightnessRange.addEventListener("input", () => {
        brightnessValue.value = brightnessRange.value;
      });
      brightnessValue.addEventListener("input", () => {
        brightnessRange.value = brightnessValue.value;
      });

      document.getElementById("applyBrightness").addEventListener("click", async () => {
        const value = parseInt(brightnessRange.value, 10);
        if (Number.isNaN(value)) {
          setLog("Brightness must be a number.", false);
          return;
        }
        try {
          const res = await fetch("/brightness", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value })
          });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Brightness failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Brightness set to ${data.brightness}.`);
          loadStatus();
        } catch (err) {
          setLog("Brightness failed: network error", false);
        }
      });

      document.getElementById("clearMatrix").addEventListener("click", async () => {
        try {
          const res = await fetch("/clear", { method: "POST" });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Clear failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog("Display cleared.");
        } catch (err) {
          setLog("Clear failed: network error", false);
        }
      });

      document.getElementById("loadDefault").addEventListener("click", async () => {
        try {
          const res = await fetch("/default/load", { method: "POST" });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Load failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Default loaded (${data.bytes} bytes).`);
          loadStatus();
        } catch (err) {
          setLog("Load failed: network error", false);
        }
      });

      document.getElementById("setCurrentDefault").addEventListener("click", async () => {
        try {
          const res = await fetch("/default/current", { method: "POST" });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Default failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Default saved (${data.bytes} bytes).`);
          loadStatus();
        } catch (err) {
          setLog("Default failed: network error", false);
        }
      });

      document.getElementById("uploadDefaultForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const fileInput = document.getElementById("defaultFile");
        const file = fileInput.files[0];
        if (!file) {
          setLog("Choose a GIF for default first.", false);
          return;
        }
        const formData = new FormData();
        formData.append("file", file);
        setLog("Uploading default...");
        try {
          const res = await fetch("/default/upload", { method: "POST", body: formData });
          const data = await res.json();
          if (!res.ok) {
            setLog(`Upload failed: ${data.detail || res.status}`, false);
            return;
          }
          setLog(`Default uploaded (${data.bytes} bytes).`);
          loadStatus();
        } catch (err) {
          setLog("Upload failed: network error", false);
        }
      });

      document.getElementById("refreshStatus").addEventListener("click", loadStatus);
      loadStatus();
      setInterval(loadStatus, 5000);
    </script>
  </body>
</html>
"""

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
    _seed_default_gif()
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
async def upload(request: Request, file: Optional[UploadFile] = File(None), set_default: bool = False):
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
        _atomic_write(PATH_LAST, data)

        # Validate GIF & save canonical copy atomically so the player never
        # reads a half-written file
        try:
            # validate
            _frames, _durations = decode_gif_frames(data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad-image:{e}")

        _atomic_write(PATH_CURR, data)

        if set_default:
            try:
                _write_default_gif(data)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"default-failed:{e}")

        # Tell the player to reload immediately
        CHANGE_EVENT.set()

        return {"ok": True, "bytes": len(data), "default_set": bool(set_default)}
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
        "clear": "curl -X POST http://<pi>:9090/clear",
        "ui": "http://<pi>:9090/ui",
        "setup": "http://<pi>:9090/setup",
        "status": "http://<pi>:9090/status"
    })

@app.get("/ui")
def ui():
    return HTMLResponse(UI_HTML)

@app.get("/setup")
def setup():
    return HTMLResponse(SETUP_HTML)

@app.get("/status")
def status():
    mw, mh = _matrix_size()
    return {
        "ok": True,
        "current": _file_info(CURRENT_GIF),
        "default": {
            "path": DEFAULT_GIF_PATH or "",
            **_file_info(DEFAULT_GIF_PATH),
        },
        "config": {
            "runtime_dir": RUN_DIR,
            "allow_nets": ALLOW_NETS_RAW,
            "rows": mh,
            "cols": mw,
            "brightness": _current_brightness(),
            "hardware_mapping": os.environ.get("LED_HARDWARE_MAPPING", "regular"),
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_frames": MAX_FRAMES,
        },
    }

@app.get("/current.gif")
def current_gif():
    if not os.path.exists(CURRENT_GIF):
        raise HTTPException(status_code=404, detail="no-current-gif")
    return FileResponse(CURRENT_GIF, media_type="image/gif", headers={"Cache-Control": "no-store"})

@app.head("/current.gif")
def head_current_gif():
    if not os.path.exists(CURRENT_GIF):
        raise HTTPException(status_code=404, detail="no-current-gif")
    return Response(status_code=200, headers=_file_stat_headers(CURRENT_GIF))

@app.post("/default/current")
def set_default_current():
    if not os.path.exists(CURRENT_GIF):
        raise HTTPException(status_code=404, detail="no-current-gif")
    try:
        with open(CURRENT_GIF, "rb") as f:
            data = f.read()
        if not data:
            raise HTTPException(status_code=400, detail="current-gif-empty")
        decode_gif_frames(data)
        _write_default_gif(data)
        return {"ok": True, "bytes": len(data), "path": DEFAULT_GIF_PATH or ""}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"default-failed:{e}")

@app.post("/default/load")
def load_default():
    if not DEFAULT_GIF_PATH or not os.path.exists(DEFAULT_GIF_PATH):
        raise HTTPException(status_code=404, detail="no-default-gif")
    try:
        with open(DEFAULT_GIF_PATH, "rb") as f:
            data = f.read()
        if not data:
            raise HTTPException(status_code=400, detail="default-gif-empty")
        decode_gif_frames(data)
        _atomic_write(PATH_CURR, data)
        CHANGE_EVENT.set()
        return {"ok": True, "bytes": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"default-load-failed:{e}")

@app.post("/default/upload")
async def upload_default(file: UploadFile = File(...)):
    try:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="upload-failed:empty-body")
        if MAX_UPLOAD_BYTES and len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload-too-large")
        try:
            _frames, _durations = decode_gif_frames(data)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad-image:{e}")
        _write_default_gif(data)
        return {"ok": True, "bytes": len(data), "path": DEFAULT_GIF_PATH or ""}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"default-upload-failed:{e}")
