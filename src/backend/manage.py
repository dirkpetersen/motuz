import os
import sys
import unittest

import click
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


@cli.group('workers')
def workers():
    """Remote workers (README, "Remote workers (HTTPS only)")"""


@workers.command('add')
@click.argument('name')
@click.option('--pool', required=True, help="The worker's pool, e.g. onprem or aws")
def workers_add(name, pool):
    """Creates a worker credential and prints its secret (shown only once)"""
    from api.managers import worker_manager
    try:
        worker, secret = worker_manager.add_worker(name, pool)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo("Worker {} (pool {}) created. Its secret, shown only now; put it in the worker's".format(worker.name, worker.pool), err=True)
    click.echo('credential file (mode 600, README "Remote workers (HTTPS only)"):', err=True)
    click.echo(secret)


@workers.command('list')
def workers_list():
    """Lists the workers and when they were last seen"""
    from api.managers import worker_manager
    for worker in worker_manager.list_workers():
        click.echo('{:<24} pool={:<10} {:<9} last seen {} from {} version {}'.format(
            worker.name, worker.pool,
            'revoked' if worker.revoked_at else ('ephemeral' if worker.ephemeral else 'active'),
            worker.last_seen_at or 'never', worker.last_seen_ip or '-', worker.version or '-'))


@workers.command('revoke')
@click.argument('name')
def workers_revoke(name):
    """Revokes a worker: its tokens stop working and its running jobs fail"""
    from api.managers import worker_manager
    try:
        worker_manager.revoke_worker(name)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo('Worker {} revoked'.format(name))


def _duration(value):
    units = {'s': 1, 'm': 60, 'h': 3600}
    value = value.strip().lower()
    if value[-1:] in units and value[:-1].isdigit():
        return int(value[:-1]) * units[value[-1]]
    if value.isdigit():
        return int(value)
    raise click.BadParameter('a duration like 900, 15m or 1h')


@workers.command('bootstrap')
@click.option('--pool', required=True, help='Pool of the ephemeral worker, e.g. aws')
@click.option('--job', default=None, help='Only this job: <id> (copy), copy:<id> or hashsum:<id>')
@click.option('--ttl', default='15m', help='Validity of the token (default 15m)')
def workers_bootstrap(pool, job, ttl):
    """Prints a single-use bootstrap token for an ephemeral worker (motuz-worker --bootstrap-token)"""
    from api.managers import worker_manager
    try:
        token = worker_manager.create_bootstrap_token(pool, job=job, ttl_seconds=_duration(ttl))
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(token)


@cli.command('test')
def test():
    """Runs the unit tests."""
    tests = unittest.TestLoader().discover('../../test/backend', pattern='test*.py')
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    cli()
