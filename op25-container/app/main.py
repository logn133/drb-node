from fastapi import FastAPI
import routers.op25_controller as op25_controller
from internal.logger import create_logger

# Initialize logging
LOGGER = create_logger(__name__)

# Define FastAPI app
app = FastAPI()

app.include_router(op25_controller.create_op25_router(), prefix="/op25")
