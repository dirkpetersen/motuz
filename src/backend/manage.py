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


@cli.group('ec2')
def ec2():
    """Temporary EC2 workers (README, "Temporary EC2 workers")"""


def _ec2_enabled():
    from api.managers import ec2_launcher
    if not ec2_launcher.settings().enabled:
        raise click.ClickException('EC2 workers are off (MOTUZ_EC2_WORKERS is not true)')
    return ec2_launcher


@ec2.command('reap')
@click.option('--loop', is_flag=True, help='Repeat every MOTUZ_EC2_REAP_INTERVAL (the Celery container runs this)')
def ec2_reap(loop):
    """Terminates finished, overdue and unknown workers, fails jobs of dead ones, launches queued jobs"""
    ec2_launcher = _ec2_enabled()
    if loop:
        ec2_launcher.reap_loop()
        return
    click.echo(ec2_launcher._summary_text(ec2_launcher.reap()))


@ec2.command('status')
@click.option('--limit', default=20, help='Number of recent launches to show')
def ec2_status(limit):
    """Recent EC2 worker launches and the worker instances EC2 reports"""
    from api.managers import ec2_launcher
    from api.models import Ec2Worker
    s = ec2_launcher.settings()
    click.echo('EC2 workers {} (pool {}, region {}, at most {}, max runtime {}s, types {})'.format(
        'on' if s.enabled else 'off', s.pool, s.region or '-', s.max_workers, s.max_runtime,
        ', '.join('<{}:{}'.format(r.limit, r.instance_type) if r.limit else '*:' + r.instance_type
                  for r in s.instance_types)))
    for row in Ec2Worker.query.order_by(Ec2Worker.id.desc()).limit(limit):
        click.echo('{:<20} {}:{:<6} #{} {:<14} {:<14} launched {} terminated {} {}'.format(
            row.instance_id or '-', row.job_type, row.job_id, row.attempt, row.instance_type, row.state,
            row.launched_at or '-', row.terminated_at or '-', row.last_error or ''))
    if s.enabled:
        for instance in ec2_launcher._describe_workers(ec2_launcher.client()):
            tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
            click.echo('EC2: {} {} {} {} job {}'.format(instance['InstanceId'], instance['InstanceType'],
                       instance['State']['Name'], instance.get('LaunchTime'), tags.get('MotuzJob', '-')))


@ec2.command('check')
def ec2_check():
    """RunInstances with DryRun for each instance type: are this node's IAM permissions right?"""
    ec2_launcher = _ec2_enabled()
    failed = False
    for instance_type, ok, message in ec2_launcher.check_permissions():
        click.echo('{:<14} {} ({})'.format(instance_type, 'allowed' if ok else 'DENIED', message))
        failed = failed or not ok
    sys.exit(1 if failed else 0)


@cli.command('test')
def test():
    """Runs the unit tests."""
    tests = unittest.TestLoader().discover('../../test/backend', pattern='test*.py')
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    cli()
