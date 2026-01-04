from fastapi import FastAPI, UploadFile, File, HTTPException
import paho.mqtt.client as mqtt
import os

app = FastAPI()

MQTT_HOST = os.getenv("MQTT_HOST","localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT","1883"))
MQTT_USER = os.getenv("MQTT_USER","")
MQTT_PASS = os.getenv("MQTT_PASS","")
TOPIC_ANIM = os.getenv("TOPIC_ANIM","home/ledmatrix/animation")

def pub_bytes(payload: bytes):
    client = mqtt.Client(protocol=mqtt.MQTTv311)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    info = client.publish(TOPIC_ANIM, payload, qos=1)
    info.wait_for_publish()
    client.loop_stop()
    client.disconnect()

@app.get("/ping")
def ping():
    return {"status":"ok"}

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        pub_bytes(data)
        return {"ok": True, "bytes": len(data), "filename": file.filename}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"publish failed: {e}")
