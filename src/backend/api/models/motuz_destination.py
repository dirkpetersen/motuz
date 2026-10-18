"""
Paths that a Motuz copy job or upload wrote to (managers/overwrite_policy.py): copies
overwrite existing files only at or below one of them, unless
MOTUZ_ALLOW_GENERAL_OVERWRITES is true. Shared by all users on purpose (a folder
another user copied counts too), so the managers never filter it by `owner`, which only
records who wrote the path first; the API reveals only whether a path is covered.
"""
import datetime

from ..application import db


class MotuzDestination(db.Model):
    __tablename__ = 'motuz_destination'
    __table_args__ = (db.UniqueConstraint('storage_key', 'path'),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    # utils/motuz_destinations.storage_key: the storage, independent of the connection
    storage_key = db.Column(db.String, nullable=False)
    # Normalized (utils/motuz_destinations.normalized_path): the file or folder written
    path = db.Column(db.String, nullable=False)
    # The job that wrote it first: 'copy' or 'upload', and its copy_job id
    job_type = db.Column(db.String, nullable=False)
    job_id = db.Column(db.Integer, nullable=True)
    owner = db.Column(db.String, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    def __repr__(self):
        return '<Motuz Destination {}{}>'.format(self.storage_key, self.path)
