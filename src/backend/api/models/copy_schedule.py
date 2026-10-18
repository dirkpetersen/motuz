from sqlalchemy import true
from sqlalchemy.orm import relationship, backref

from ..application import db
from ..mixins.timestamp_mixin import TimestampMixin


class CopySchedule(db.Model, TimestampMixin):
    """
    A scheduled repeating copy (managers/schedule_manager.py, README "Scheduled
    copies"): the parameters of its copy jobs and when they run. Every run is a normal
    copy job (copy_job.schedule_id) that only adds new files: it never deletes and never
    overwrites unless MOTUZ_ALLOW_GENERAL_OVERWRITES is true.
    """
    __tablename__ = "copy_schedule"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    owner = db.Column(db.String, index=True, nullable=False)

    # The copy job's parameters
    description = db.Column(db.String)
    src_cloud_id = db.Column(db.Integer, db.ForeignKey('cloud_connection.id'))
    src_resource_path = db.Column(db.String, nullable=False)
    dst_cloud_id = db.Column(db.Integer, db.ForeignKey('cloud_connection.id'))
    dst_resource_path = db.Column(db.String, nullable=False)
    copy_links = db.Column(db.Boolean)
    notification_email = db.Column(db.String)
    # Validated rclone performance overrides (utils/rclone_tuning.py), NULL = defaults
    performance = db.Column(db.JSON, nullable=True)

    # When it runs (utils/schedule.py), all UTC: 'hourly' | 'daily' | 'weekly' |
    # 'monthly'; minute 0-59, hour 0-23 (daily and longer), weekday 0-6 = Monday-Sunday
    # (weekly), day_of_month 1-28 (monthly); unused fields NULL
    frequency = db.Column(db.String, nullable=False)
    minute = db.Column(db.Integer, nullable=False)
    hour = db.Column(db.Integer, nullable=True)
    weekday = db.Column(db.Integer, nullable=True)
    day_of_month = db.Column(db.Integer, nullable=True)

    enabled = db.Column(db.Boolean, nullable=False, server_default=true())
    # The next run (UTC); the runner claims schedules whose time has come
    next_run_at = db.Column(db.DateTime, nullable=False, index=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    # The copy job of the last run, and what happened at the last due time ("Started
    # job 12", "Skipped: job 12 was still running", "Not run: <why>")
    last_job_id = db.Column(db.Integer, nullable=True)
    last_status = db.Column(db.String, nullable=True)
    skipped_runs = db.Column(db.Integer, nullable=False, server_default='0')

    # The AWS egress cost the user confirmed for the schedule (the first run's estimate
    # and the monthly one, managers/schedule_manager.py); NULL when no AWS fee applies.
    # Each run's job stores it again with schedule_id, without a new confirmation.
    egress_estimate = db.Column(db.JSON, nullable=True)

    src_cloud = relationship(
        "CloudConnection",
        foreign_keys=[src_cloud_id],
        backref=backref("src_copy_schedules", cascade="all,delete"),
    )
    dst_cloud = relationship(
        "CloudConnection",
        foreign_keys=[dst_cloud_id],
        backref=backref("dst_copy_schedules", cascade="all,delete"),
    )

    def __repr__(self):
        return "<Copy Schedule {}>".format(self.id)
