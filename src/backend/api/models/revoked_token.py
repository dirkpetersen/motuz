from ..application import db


class RevokedToken(db.Model):
    __tablename__ = 'revoked_token'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    jti = db.Column(db.String, unique=True, nullable=False)
    type = db.Column(db.String, nullable=False)
    identity = db.Column(db.String, nullable=False)
    exp = db.Column(db.Integer, nullable=False)
    # A rotated refresh token still works until then (tabs refreshing concurrently).
    # NULL: revoked now. type 'session' rows hold a session id (JWT claim sid) as jti.
    grace_until = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return '<jti: {}>'.format(self.jti)
