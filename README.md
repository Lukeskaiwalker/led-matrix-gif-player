# LED Matrix GIF Player

FastAPI service that receives a GIF over HTTP and loops it on an RGB LED matrix.
The service running on the Pi is `ledmatrix_http_player.py`.

## Endpoints
- `GET /ping`
- `POST /upload` (raw bytes or multipart)
- `POST /brightness` with JSON `{ "value": 1..100 }`
- `POST /clear`
- `GET /ui` (web UI)
- `GET /setup` (setup UI)
- `GET /current.gif` (preview of current GIF)
- `GET /status` (JSON status/config snapshot)
- `POST /default/current` (save current GIF as default)
- `POST /default/load` (load default GIF into the player)
- `POST /default/upload` (upload default GIF without playing)

### Examples
```bash
curl --data-binary @/home/pi/test.gif http://<pi>:9090/upload
curl -F 'file=@/home/pi/test.gif;type=image/gif' http://<pi>:9090/upload
curl -X POST -H 'Content-Type: application/json' -d '{"value":60}' http://<pi>:9090/brightness
curl -X POST http://<pi>:9090/clear
curl -X POST http://<pi>:9090/default/current
curl -X POST http://<pi>:9090/default/load
curl -F 'file=@/home/pi/test.gif;type=image/gif' http://<pi>:9090/default/upload
```

## Configuration (env vars)
- `LED_RUNTIME_DIR` (default `/run/ledmatrix`)
- `ALLOW_NETS` (comma-separated CIDR allowlist)
- `DEFAULT_GIF_PATH` (path to GIF used on startup)
- `LED_ROWS`, `LED_COLS`, `LED_BRIGHTNESS`, `LED_HARDWARE_MAPPING`
- `LED_NO_HARDWARE_PULSE` (`1` to disable hardware pulsing)
- `MAX_UPLOAD_BYTES` (optional upload size limit)
- `MAX_FRAMES` (optional frame count limit)

## Web UI
Open `http://<pi>:9090/ui` for a live preview and manual upload.
Open `http://<pi>:9090/setup` for brightness controls, defaults, and system status.

## Run locally on the Pi
```bash
python -m uvicorn ledmatrix_http_player:app --host 0.0.0.0 --port 9090
```

## Systemd
See `systemd/ledmatrix-http.service` for a working unit file.

## Other scripts
- `ledmatrix_mqtt_gif.py`: MQTT-based player.
- `gif_uploader_http.py`: HTTP uploader that publishes to MQTT.
