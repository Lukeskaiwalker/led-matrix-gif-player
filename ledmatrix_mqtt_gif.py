#!/usr/bin/env python3
import os, io, base64, time, threading, signal, traceback
from dataclasses import dataclass
from PIL import Image, ImageFile, ImageSequence
ImageFile.LOAD_TRUNCATED_IMAGES = True

import paho.mqtt.client as mqtt
from rgbmatrix import RGBMatrix, RGBMatrixOptions

# ---------- Config via env ----------
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
TOPIC_ANIM = os.getenv("TOPIC_ANIM", "home/ledmatrix/animation")
TOPIC_CMD  = os.getenv("TOPIC_CMD",  "home/ledmatrix/cmd")
TOPIC_STAT = os.getenv("TOPIC_STAT", "home/ledmatrix/status")

LED_ROWS = int(os.getenv("LED_ROWS", "64"))
LED_COLS = int(os.getenv("LED_COLS", "64"))
LED_CHAIN = int(os.getenv("LED_CHAIN", "1"))
LED_PARALLEL = int(os.getenv("LED_PARALLEL", "1"))
LED_PWM_BITS = int(os.getenv("LED_PWM_BITS", "11"))
LED_BRIGHTNESS = int(os.getenv("LED_BRIGHTNESS", "80"))
LED_HARDWARE_MAPPING = os.getenv("LED_HARDWARE_MAPPING", "regular")
LED_SCAN_MODE = int(os.getenv("LED_SCAN_MODE", "0"))
LED_ROW_ADDR_TYPE = int(os.getenv("LED_ROW_ADDR_TYPE", "0"))
LED_PWM_LSB_NANOS = int(os.getenv("LED_PWM_LSB_NANOS", "130"))
LED_SHOW_REFRESH = int(os.getenv("LED_SHOW_REFRESH", "0"))
LED_INVERSE = int(os.getenv("LED_INVERSE", "0"))
LED_NO_HW_PULSE = os.getenv("LED_NO_HARDWARE_PULSE", "0") == "1"

TMP_GIF = "/tmp/ledmatrix_current.gif"

play_lock = threading.Lock()
stop_event = threading.Event()
current_player = None

@dataclass
class Frame:
    img: Image.Image
    duration_ms: int

def _maybe_base64(data: bytes) -> bytes:
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        try: return base64.b64decode(data, validate=True)
        except Exception: return data
    return data

def _salvage_gif(data: bytes) -> bytes:
    h87 = data.find(b"GIF87a"); h89 = data.find(b"GIF89a")
    heads = [x for x in (h87, h89) if x != -1]
    if not heads: return data
    start = min(heads)
    end = data.rfind(b"\x3B")  # GIF trailer
    if end != -1 and end >= start: return data[start:end+1]
    return data[start:]

class GifPlayer:
    def __init__(self, matrix: RGBMatrix):
        self.matrix = matrix
        self.canvas = matrix.CreateFrameCanvas()
        self.running = False

    def _extract_frames(self, im: Image.Image):
        frames = []
        # tolerant frame iterator
        for frm in ImageSequence.Iterator(im):
            fr = frm.convert("RGB")
            if fr.size != (LED_COLS, LED_ROWS):
                fr = fr.resize((LED_COLS, LED_ROWS), Image.NEAREST)
            duration = frm.info.get("duration", im.info.get("duration", 50))
            try: duration = int(duration)
            except Exception: duration = 50
            duration = max(10, min(1000, duration))
            frames.append(Frame(fr, duration))
        if not frames:
            fr = im.convert("RGB")
            if fr.size != (LED_COLS, LED_ROWS):
                fr = fr.resize((LED_COLS, LED_ROWS), Image.NEAREST)
            frames.append(Frame(fr, 500))
        return frames

    def _play_frames(self, frames):
        self.running = True
        stop_event.clear()
        try:
            while self.running and not stop_event.is_set():
                for fr in frames:
                    if stop_event.is_set() or not self.running: break
                    self.canvas.SetImage(fr.img, 0, 0)
                    self.canvas = self.matrix.SwapOnVSync(self.canvas)
                    time.sleep(fr.duration_ms / 1000.0)
        finally:
            self.running = False

    def play_path(self, path: str):
        # Try Pillow from path
        try:
            im = Image.open(path)
            frames = self._extract_frames(im)
            return self._play_frames(frames)
        except Exception:
            pass

        # Fallback 1: salvage bytes then Pillow
        try:
            with open(path, "rb") as f:
                raw = f.read()
            b = _salvage_gif(_maybe_base64(raw))
            im = Image.open(io.BytesIO(b))
            frames = self._extract_frames(im)
            return self._play_frames(frames)
        except Exception:
            pass

        # Fallback 2: imageio (very robust on odd GIFs)
        try:
            import imageio.v3 as iio
            frames_np = list(iio.imiter(path))
            frames = []
            for arr in frames_np:
                fr = Image.fromarray(arr).convert("RGB")
                if fr.size != (LED_COLS, LED_ROWS):
                    fr = fr.resize((LED_COLS, LED_ROWS), Image.NEAREST)
                frames.append(Frame(fr, 50))  # default 50ms if duration not available
            if frames:
                return self._play_frames(frames)
            raise RuntimeError("imageio decoded 0 frames")
        except Exception as e:
            raise e

    def stop(self): self.running = False

def build_matrix():
    opts = RGBMatrixOptions()
    opts.rows = LED_ROWS; opts.cols = LED_COLS
    opts.chain_length = LED_CHAIN; opts.parallel = LED_PARALLEL
    opts.hardware_mapping = LED_HARDWARE_MAPPING
    opts.pwm_bits = LED_PWM_BITS; opts.brightness = LED_BRIGHTNESS
    opts.scan_mode = LED_SCAN_MODE; opts.row_address_type = LED_ROW_ADDR_TYPE
    opts.pwm_lsb_nanoseconds = LED_PWM_LSB_NANOS
    opts.show_refresh_rate = LED_SHOW_REFRESH
    opts.inverse_colors = bool(LED_INVERSE)
    if LED_NO_HW_PULSE:
        for attr in ("disable_hardware_pulsing", "no_hardware_pulse"):
            try: setattr(opts, attr, True); break
            except Exception: pass
    return RGBMatrix(options=opts)

def on_connect(client, userdata, flags, reason_code, properties=None):
    rc = getattr(reason_code, "value", reason_code)
    client.publish(TOPIC_STAT, "connected" if rc == 0 else f"connect_failed:{rc}", qos=1)
    client.subscribe([(TOPIC_ANIM, 0), (TOPIC_CMD, 0)])

def on_message(client, userdata, msg):
    global current_player
    payload = msg.payload

    if msg.topic == TOPIC_CMD:
        txt = payload.decode(errors="ignore").strip().lower()
        if txt.startswith("brightness:"):
            try:
                val = max(1, min(100, int(txt.split(":", 1)[1])))
                userdata["matrix"].brightness = val
                client.publish(TOPIC_STAT, f"brightness:{val}")
            except Exception as e:
                client.publish(TOPIC_STAT, f"error:brightness:{e}")
        elif txt == "clear":
            canvas = userdata["matrix"].CreateFrameCanvas(); canvas.Clear()
            userdata["matrix"].SwapOnVSync(canvas)
            client.publish(TOPIC_STAT, "cleared")
        elif txt == "stop":
            stop_event.set()
            if current_player: current_player.stop()
            client.publish(TOPIC_STAT, "stopped")
        elif txt == "ping":
            client.publish(TOPIC_STAT, "pong")
        else:
            client.publish(TOPIC_STAT, f"unknown_cmd:{txt}")
        return

    if msg.topic == TOPIC_ANIM:
        # Write exactly what we'll open, and fsync to avoid partial reads
        try:
            with open("/tmp/last_ledmatrix_payload.bin", "wb") as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            with open(TMP_GIF, "wb") as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
        except Exception:
            pass

        client.publish(TOPIC_STAT, f"received:{len(payload)}")

        def runner(data_in: bytes):
            global current_player
            try:
                # Try to count frames quickly from path (best-effort)
                try:
                    im = Image.open(TMP_GIF)
                    n = getattr(im, "n_frames", 1)
                    client.publish(TOPIC_STAT, f"frames:{n}")
                except Exception:
                    client.publish(TOPIC_STAT, "frames:?")

                with play_lock:
                    stop_event.set()
                    if current_player: current_player.stop()
                    time.sleep(0.05)
                    stop_event.clear()
                    current_player = GifPlayer(userdata["matrix"])

                current_player.play_path(TMP_GIF)

            except Exception as e:
                hdr = data_in[:16].hex()
                client.publish(TOPIC_STAT, f"error:play:{e.__class__.__name__}:{e};hdr={hdr}")
                print("PLAYBACK ERROR:", "".join(traceback.format_exception(e)).strip())

        threading.Thread(target=runner, args=(payload,), daemon=True).start()
        client.publish(TOPIC_STAT, "playing")

def main():
    matrix = build_matrix()
    userdata = {"matrix": matrix}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="ledmatrix-player",
                         userdata=userdata,
                         protocol=mqtt.MQTTv311)
    if MQTT_USER: client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    signal.signal(signal.SIGINT,  lambda *_: os._exit(0))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever()

if __name__ == "__main__":
    main()
