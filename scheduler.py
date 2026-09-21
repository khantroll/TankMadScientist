"""
Cron-style scheduling, backed by APScheduler in-process (no extra
infra needed). Schedules live in the DB; on trigger, a schedule just
creates a pending run, which the queue worker then picks up — so
scheduled jobs go through the exact same path as manual ones.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import models

scheduler = BackgroundScheduler()


def _trigger_schedule(schedule_id):
    schedule = models.get_schedule(schedule_id)
    if not schedule or not schedule["enabled"]:
        return
    models.create_run(
        schedule["workspace_id"],
        schedule["role_id"],
        schedule["task"],
        provider=schedule["provider"],
    )
    models.touch_schedule(schedule_id)


def reload_jobs():
    """Rebuild all APScheduler jobs from the current DB state."""
    for job in scheduler.get_jobs():
        job.remove()
    for schedule in models.list_schedules(enabled_only=True):
        try:
            trigger = CronTrigger.from_crontab(schedule["cron_expr"])
        except ValueError:
            continue  # bad cron expression — skip rather than crash the app
        scheduler.add_job(
            _trigger_schedule,
            trigger=trigger,
            args=[schedule["id"]],
            id=f"schedule-{schedule['id']}",
            replace_existing=True,
        )


def start():
    reload_jobs()
    if not scheduler.running:
        scheduler.start()
