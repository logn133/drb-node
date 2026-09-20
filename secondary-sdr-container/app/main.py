from fastapi import FastAPI
import routers.secondary_controller as secondary_controller
from config import bind_host

app = FastAPI()

app.include_router(secondary_controller.create_secondary_router(), prefix="/secondary")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=bind_host(), port=8002, reload=True)
