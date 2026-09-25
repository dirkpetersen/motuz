import os

basedir = os.path.abspath(os.path.dirname(__file__))


# Preventing lazy loading of mandatory variables
try:
    MOTUZ_FLASK_SECRET_KEY = os.environ['MOTUZ_FLASK_SECRET_KEY']
    MOTUZ_DATABASE_PROTOCOL = os.environ['MOTUZ_DATABASE_PROTOCOL']
    MOTUZ_DATABASE_USER = os.environ['MOTUZ_DATABASE_USER']
    MOTUZ_DATABASE_PASSWORD = os.environ['MOTUZ_DATABASE_PASSWORD']
    MOTUZ_DATABASE_NAME = os.environ['MOTUZ_DATABASE_NAME']
    MOTUZ_DATABASE_HOST = os.environ['MOTUZ_DATABASE_HOST']
    MOTUZ_DATABASE_REQUIRE_SSL = os.environ.get('MOTUZ_DATABASE_REQUIRE_SSL', 'false')
except KeyError as e:
    raise KeyError("Environment variable {} not set".format(e.args[0]))



class Config:
    SECRET_KEY = MOTUZ_FLASK_SECRET_KEY
    JWT_SECRET_KEY = MOTUZ_FLASK_SECRET_KEY
    JWT_IDENTITY_CLAIM = 'identity' # Frontend reads `identity`, and keeps pre-upgrade tokens valid
    CELERY_BROKER_URL = 'amqp://'

    DATABASE_PARAMS = ''
    if MOTUZ_DATABASE_REQUIRE_SSL.lower() in ('true', 't'):
        DATABASE_PARAMS = '?sslmode=require'
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
    SQLALCHEMY_DATABASE_URI = '{PROTOCOL}://{USER}:{PASSWORD}@{HOST}/{DATABASE}{PARAMS}'.format(
        PROTOCOL=MOTUZ_DATABASE_PROTOCOL,
        USER=MOTUZ_DATABASE_USER,
        PASSWORD=MOTUZ_DATABASE_PASSWORD,
        HOST=MOTUZ_DATABASE_HOST,
        DATABASE=MOTUZ_DATABASE_NAME,
        PARAMS=DATABASE_PARAMS,
    )
    # Task progress is written by the worker and read by the API, so results must be
    # stored somewhere shared. The amqp result backend was removed in Celery 5.
    CELERY_RESULT_BACKEND = 'db+' + SQLALCHEMY_DATABASE_URI

    # Where rclone refreshes OAuth tokens (managers/token_broker_manager.py). Loopback
    # HTTP socket of uWSGI in production, the Flask dev server in development.
    TOKEN_BROKER_URL = os.environ.get('MOTUZ_TOKEN_BROKER_URL', 'http://127.0.0.1:5001/internal/oauth/token')
    # Upstream token endpoints the broker refreshes against
    ONEDRIVE_TOKEN_URL = os.environ.get('MOTUZ_ONEDRIVE_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token')

    # "Sign in with Microsoft" (managers/oauth_manager.py). Defaults to rclone's public
    # OneDrive app, whose only redirect URI is http://localhost:53682/: the user then pastes
    # the address the browser was redirected to. With an own app registration whose redirect
    # URI is https://<motuz host>/api/oauth/onedrive/callback the flow completes by itself.
    ONEDRIVE_CLIENT_ID = os.environ.get('MOTUZ_ONEDRIVE_CLIENT_ID')
    ONEDRIVE_CLIENT_SECRET = os.environ.get('MOTUZ_ONEDRIVE_CLIENT_SECRET')
    ONEDRIVE_REDIRECT_URI = os.environ.get('MOTUZ_ONEDRIVE_REDIRECT_URI', 'http://localhost:53682/')
    ONEDRIVE_AUTH_URL = os.environ.get('MOTUZ_ONEDRIVE_AUTH_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize')
    GRAPH_URL = os.environ.get('MOTUZ_GRAPH_URL', 'https://graph.microsoft.com/v1.0')

    # Built frontend (`npm run build`), served by views/frontend_views.py. The app image
    # puts it at /app/build, which is also <repository>/build in development.
    FRONTEND_DIR = os.environ.get('MOTUZ_FRONTEND_DIR', os.path.abspath(os.path.join(basedir, '..', '..', '..', 'build')))

    DEBUG = False
    # https://flask-sqlalchemy.palletsprojects.com/en/2.x/signals/
    SQLALCHEMY_TRACK_MODIFICATIONS = False



class DevelopmentConfig(Config):
    DEBUG = True
    TOKEN_BROKER_URL = os.environ.get('MOTUZ_TOKEN_BROKER_URL', 'http://127.0.0.1:5000/internal/oauth/token')



class TestingConfig(Config):
    DEBUG = True
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'database_test.sqlite3')
    CELERY_RESULT_BACKEND = 'db+' + SQLALCHEMY_DATABASE_URI
    PRESERVE_CONTEXT_ON_EXCEPTION = False



class ProductionConfig(Config):
    DEBUG = False



config_by_name = dict(
    dev=DevelopmentConfig,
    test=TestingConfig,
    prod=ProductionConfig
)
