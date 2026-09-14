import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tesi_project.settings")

app = Celery("tesi_project")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
