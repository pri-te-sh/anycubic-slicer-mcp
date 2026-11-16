# Lets directories such as Glama start the server and list its tools in a Linux container.
# Slicing itself needs Anycubic Slicer Next, which runs on Windows and macOS, so inside the
# container slicer_status reports that the slicer isn't installed; every tool still loads.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY . /app
RUN pip install /app && useradd --create-home mcp
USER mcp
ENTRYPOINT ["anycubic-slicer-mcp"]
