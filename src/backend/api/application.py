import logging
import os

from celery import Celery
from flask import Flask, Blueprint
from flask_sqlalchemy import SQLAlchemy
from flask_restx import Api
from flask_jwt_extended import JWTManager

from .config import config_by_name, Config

logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] %(levelname)s: %(message)s')

db = SQLAlchemy()

app = Flask(__name__, instance_relative_config=True)
# The Celery worker imports this module but never calls create_app(), so the
# configuration must already be the one selected for this environment
app.config.from_object(config_by_name[os.getenv('PYTHON_ENVIRONMENT') or 'dev'])

db.init_app(app)
jwt = JWTManager(app)

celery = Celery(
    __name__,
    backend=Config.CELERY_RESULT_BACKEND,
    broker=Config.CELERY_BROKER_URL,
)
celery.conf.update(
    broker_connection_retry_on_startup=True,
)

class ContextTask(celery.Task):
    def __call__(self, *args, **kwargs):
        with app.app_context():
            return self.run(*args, **kwargs)

celery.Task = ContextTask


def create_app(config_name='dev'):
    global app
    app.config.from_object(config_by_name[config_name])

    # create_app may run more than once per process (manage.py test, wsgi), and Flask
    # refuses to register blueprints again once a request has been handled
    if 'api' not in app.blueprints:
        register_api(app)

    from .views.internal_views import bp as internal_bp
    if 'internal' not in app.blueprints:
        app.register_blueprint(internal_bp)

    # Public privacy policy and terms (no login, no JavaScript)
    from .views.legal_views import bp as legal_bp
    if 'legal' not in app.blueprints:
        app.register_blueprint(legal_bp)

    # Werkzeug prefers rules with more static parts, so the frontend's catch-all route
    # only gets paths no other rule matches (trailing-slash redirects are kept)
    from .views.frontend_views import bp as frontend_bp
    if 'frontend' not in app.blueprints:
        app.register_blueprint(frontend_bp)

    return app


def register_api(app):
    bp = Blueprint('api', __name__, url_prefix='/api')

    api = Api(bp,
        title='Motuz',
        description='A web based infrastructure for large scale data movements '
            'between on-premise and cloud',
        version='0.0.2',
        security='Bearer Auth',
        authorizations={
            'Bearer Auth': {
                'type': 'apiKey',
                'in': 'header',
                'name': 'Authorization'
            },
        },
        ordered=True,
        terms_url='https://github.com/FredHutch/motuz',
        license='MIT',
        license_url='https://github.com/FredHutch/motuz/blob/master/LICENSE',
    )

    from .views.auth_views import api as auth_ns
    api.add_namespace(auth_ns)

    from .views.copy_job_views import api as copy_job_ns
    api.add_namespace(copy_job_ns)

    from .views.hashsum_job_views import api as hashsum_job_ns
    api.add_namespace(hashsum_job_ns)

    from .views.cloud_connection_views import api as connection_ns
    api.add_namespace(connection_ns)

    from .views.system_views import api as system_ns
    api.add_namespace(system_ns)

    from .views.oauth_views import api as oauth_ns
    api.add_namespace(oauth_ns)

    app.register_blueprint(bp)

