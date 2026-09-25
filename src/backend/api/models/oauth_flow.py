import datetime

from ..application import db


class OauthFlow(db.Model):
    """
    A pending "Sign in with Microsoft" flow (see managers/oauth_manager.py). Everything
    that must not reach the browser (PKCE verifier, tokens) stays in this row.
    """
    __tablename__ = "oauth_flow"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    state = db.Column(db.String, nullable=False, unique=True)
    owner = db.Column(db.String, nullable=False, index=True)
    provider = db.Column(db.String, nullable=False)
    code_verifier = db.Column(db.String, nullable=False)
    # App registration the sign-in started with; copied to the connection
    client_id = db.Column(db.String, nullable=True)

    # Set once the authorization code was exchanged
    token = db.Column(db.String, nullable=True)
    drives = db.Column(db.String, nullable=True) # JSON list of discovered drives

    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return "<OAuth flow {} ({})>".format(self.id, self.provider)
