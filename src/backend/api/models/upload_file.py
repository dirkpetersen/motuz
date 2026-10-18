from sqlalchemy.orm import relationship, backref

from ..application import db


class UploadFile(db.Model):
    """
    One file of an upload from the browser (CopyJob.upload): its path below the job's
    destination, its size, and how far it got. `state`: 'pending' (not yet complete),
    'done', 'skipped' (existed, skip mode) or 'failed' (`error` has rclone's message)
    """
    __tablename__ = "upload_file"
    __table_args__ = (db.UniqueConstraint('copy_job_id', 'index'),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    copy_job_id = db.Column(db.Integer, db.ForeignKey('copy_job.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    index = db.Column(db.Integer, nullable=False)
    path = db.Column(db.String, nullable=False)
    size = db.Column(db.BigInteger, nullable=False)
    bytes_done = db.Column(db.BigInteger, nullable=False, server_default="0", default=0)
    state = db.Column(db.String, nullable=False, server_default="pending", default='pending')
    error = db.Column(db.String, nullable=True)

    copy_job = relationship(
        "CopyJob",
        backref=backref("upload_files", cascade="all,delete"),
    )

    def __repr__(self):
        return "<Upload File {} of Copy Job {}>".format(self.index, self.copy_job_id)
