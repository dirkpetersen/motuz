from sqlalchemy.orm import relationship, backref

from ..application import db
from ..mixins.timestamp_mixin import TimestampMixin


class CopyJob(db.Model, TimestampMixin):
    __tablename__ = "copy_job"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    description = db.Column(db.String)
    src_cloud_id = db.Column(db.Integer, db.ForeignKey('cloud_connection.id'))
    src_resource_path = db.Column(db.String)
    dst_cloud_id = db.Column(db.Integer, db.ForeignKey('cloud_connection.id'))
    dst_resource_path = db.Column(db.String)

    # Options
    copy_links = db.Column(db.Boolean)
    notification_email = db.Column(db.String)
    # rclone performance overrides of this job (utils/rclone_tuning.py), validated;
    # NULL or {} = the installation's defaults
    performance = db.Column(db.JSON, nullable=True)
    # Upload from the user's browser (managers/upload_manager.py), else NULL:
    # {"files": count, "bytes": total, "mode": "overwrite"|"skip", "name": shown as the
    # source}; its files are the job's upload_file rows
    upload = db.Column(db.JSON, nullable=True)
    # The free space check at create (managers/space_manager.py): "Free space checked:
    # ..." or "Free space not checked: <why>"; NULL if none ran (cloud destination,
    # limit off, a remote worker's destination)
    space_check = db.Column(db.String, nullable=True)

    owner = db.Column(db.String, index=True)

    progress_state = db.Column(db.String, nullable=True)
    progress_current = db.Column(db.Integer, nullable=True)
    progress_total = db.Column(db.Integer, nullable=True)
    progress_error = db.Column(db.String, nullable=True)
    progress_execution_time = db.Column(db.Integer, nullable=False, server_default="0")

    src_cloud = relationship(
        "CloudConnection",
        foreign_keys=[src_cloud_id],
        backref=backref("src_copy_jobs", cascade="all,delete"),
    )
    dst_cloud = relationship(
        "CloudConnection",
        foreign_keys=[dst_cloud_id],
        backref=backref("dst_copy_jobs", cascade="all,delete"),
    )

    def __repr__(self):
        return "<Copy Job {}>".format(self.id)
