FROM python:3.11-slim
RUN pip install --no-cache-dir fastapi==0.139.2 redis==8.1.0 celery==5.6.3
WORKDIR /probe
COPY . ./
CMD ["sleep", "infinity"]
