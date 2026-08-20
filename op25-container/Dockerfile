# OP25 Core Container
# Pinned to a Python major version deliberately. The bare `slim-trixie` tag
# carries no version at all, so a rebuild could move the interpreter across a
# major release -- which this repo has already been bitten by once, when
# app/models.py only ran because trixie happened to ship 3.14 and PEP 649
# defers annotation evaluation. Matches drb-edge-node, which is already 3.14.
FROM python:3.14-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install git pulseaudio pulseaudio-utils liquidsoap -y

# Install custom PulseAudio system config (enables anonymous access for edge-node)
COPY system.pa /etc/pulse/system.pa

# Clone the boatbod op25 repository
RUN git clone -b gr310 https://github.com/boatbod/op25 /op25

# Set the working directory
WORKDIR /op25

# Run the install script to set up op25
RUN sed -i 's/sudo //g' install.sh
RUN ./install.sh -f

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Create the run_multi-rx_service.sh script
COPY run_multi-rx_service.sh /op25/op25/gr-op25_repeater/apps/run_multi-rx_service.sh
RUN chmod +x /op25/op25/gr-op25_repeater/apps/run_multi-rx_service.sh

# Expose ports for HTTP control as needed, for example:
EXPOSE 8001 8081

# Create and set up the configuration directory
VOLUME ["/configs"]

# Set the working directory in the container
WORKDIR /app

# Copy the rest of the directory contents into the container at /app
COPY ./app /app

# 1. Copy the wrapper script and make it executable
COPY docker-entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# 2. Update ENTRYPOINT to use the wrapper script
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# 3. Use CMD to pass the launch command as arguments to the ENTRYPOINT script.
# main.py starts uvicorn itself (see `if __name__ == "__main__"`) so the bind
# address can be driven by OP25_DEBUG_EXPOSE at runtime instead of being baked
# into this image at build time.
CMD ["python", "main.py"]