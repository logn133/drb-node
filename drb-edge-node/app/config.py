from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Node identity
    node_id: str
    node_name: str = "Unnamed Node"
    node_lat: float = 0.0
    node_lon: float = 0.0

    # MQTT
    mqtt_broker: str
    mqtt_port: int = 1883
    mqtt_user: Optional[str] = None
    mqtt_pass: Optional[str] = None

    # C2 server (audio upload destination); None disables upload
    c2_url: Optional[str] = None

    # Local Icecast
    icecast_host: str = "localhost"
    icecast_port: int = 8000
    icecast_mount: str = "/radio"
    icecast_source_password: str = "hackme"

    # OP25 container
    op25_api_url: str = "http://localhost:8001"
    op25_terminal_url: str = "http://localhost:8081"

    # Paths (volume mounts)
    config_path: str = "/configs"
    recordings_path: str = "/recordings"

    # Offline call buffer — how many call_end events to keep while disconnected
    offline_call_buffer_size: int = 35

    class Config:
        env_file = ".env"


settings = Settings()
