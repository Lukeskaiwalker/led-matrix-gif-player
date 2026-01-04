# LED Matrix GIF Player

FastAPI service that receives a GIF over HTTP and loops it on an RGB LED matrix.
The service running on the Pi is `ledmatrix_http_player.py`.

## Endpoints
- `GET /ping`
- `POST /upload` (raw bytes or multipart)
- `POST /brightness` with JSON `{ "value": 1..100 }`
- `POST /clear`

### Examples
```bash
curl --data-binary @/home/pi/test.gif http://<pi>:9090/upload
curl -F 'file=@/home/pi/test.gif;type=image/gif' http://<pi>:9090/upload
curl -X POST -H 'Content-Type: application/json' -d '{"value":60}' http://<pi>:9090/brightness
curl -X POST http://<pi>:9090/clear
```

## Configuration (env vars)
- `LED_RUNTIME_DIR` (default `/run/ledmatrix`)
- `ALLOW_NETS` (comma-separated CIDR allowlist)
- `LED_ROWS`, `LED_COLS`, `LED_BRIGHTNESS`, `LED_HARDWARE_MAPPING`
- `LED_NO_HARDWARE_PULSE` (`1` to disable hardware pulsing)
- `MAX_UPLOAD_BYTES` (optional upload size limit)
- `MAX_FRAMES` (optional frame count limit)

## Run locally on the Pi
```bash
python -m uvicorn ledmatrix_http_player:app --host 0.0.0.0 --port 9090
```

## Systemd
See `systemd/ledmatrix-http.service` for a working unit file.

## Other scripts
- `ledmatrix_mqtt_gif.py`: MQTT-based player.
- `gif_uploader_http.py`: HTTP uploader that publishes to MQTT.
