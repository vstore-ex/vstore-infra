from celery import Celery
from kombu import Queue

from app.core.config import settings

celery_app = Celery(
    "vstore_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_send_task_events=True,
    result_extended=True,
    task_default_queue="default",
    task_queues=[
        Queue("default", routing_key="default.#"),
        Queue("media", routing_key="media.#"),
    ],
    task_routes={
        "process_video_async": {"queue": "media"},
        "upload_and_process_video_async": {"queue": "media"},
        "*": {"queue": "default"},
    },
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    beat_schedule={
        "sync-game-views-every-5-minutes": {
            "task": "sync_game_views_to_db",
            "schedule": 300.0,
        },
    },
)


if __name__ == "__main__":
    celery_app.start()
