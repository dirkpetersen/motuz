import os
import sys
import unittest

from flask.cli import FlaskGroup
from flask_migrate import Migrate

from api import create_app, db
from api.models import * # To ensure that all models are tracked

app = create_app(os.getenv('PYTHON_ENVIRONMENT') or 'dev')

app.app_context().push()

migrate = Migrate(app, db)

# Provides `python manage.py db ...` (Flask-Migrate) next to the commands below
cli = FlaskGroup(create_app=lambda: app)


@cli.command('run')
def run():
    # FlaskGroup sets FLASK_RUN_FROM_CLI, which turns app.run() into a no-op
    os.environ.pop('FLASK_RUN_FROM_CLI', None)
    app.run(host=os.getenv('MOTUZ_HOST', 'localhost'))


@cli.command('test')
def test():
    """Runs the unit tests."""
    tests = unittest.TestLoader().discover('../../test/backend', pattern='test*.py')
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    cli()
