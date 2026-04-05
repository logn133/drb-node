import os

# Satisfy pydantic-settings required fields before any app module is imported.
os.environ.setdefault("NODE_ID", "test-node-01")
os.environ.setdefault("MQTT_BROKER", "localhost")
