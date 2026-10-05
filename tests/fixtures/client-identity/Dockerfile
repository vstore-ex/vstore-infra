FROM python:3.11-slim
RUN pip install --no-cache-dir fastapi==0.139.2 redis==8.1.0 uvicorn==0.51.0
WORKDIR /probe
COPY rate_limit.py probe.py client.py ./
