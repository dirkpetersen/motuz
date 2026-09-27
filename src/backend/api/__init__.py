# The Flask application (and with it the configuration, which requires the MOTUZ_*
# environment variables) is loaded on first use, so that the rclone and job code in
# api.utils can be imported without it: motuz-worker (src/worker) runs that code on
# remote workers, which have no database and no server secrets.
_APPLICATION_NAMES = ('create_app', 'db', 'celery')


def __getattr__(name):
    if name in _APPLICATION_NAMES:
        from . import application
        return getattr(application, name)
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))
