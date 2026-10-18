class HTTP_EXCEPTION(Exception):
    code = -1

    def __init__(self, payload={}):
        self.payload = payload


class HTTP_4XX_EXCEPTION(HTTP_EXCEPTION):
    pass

class HTTP_5XX_EXCEPTION(HTTP_EXCEPTION):
    pass


# 4xx Codes

class HTTP_400_BAD_REQUEST(HTTP_4XX_EXCEPTION):
    code = 400

class HTTP_401_UNAUTHORIZED(HTTP_4XX_EXCEPTION):
    code = 401

class HTTP_402_PAYMENT_REQUIRED(HTTP_4XX_EXCEPTION):
    code = 402

class HTTP_403_FORBIDDEN(HTTP_4XX_EXCEPTION):
    code = 403

class HTTP_404_NOT_FOUND(HTTP_4XX_EXCEPTION):
    code = 404

class HTTP_405_METHOD_NOT_ALLOWED(HTTP_4XX_EXCEPTION):
    code = 405

class HTTP_409_CONFLICT(HTTP_4XX_EXCEPTION):
    code = 409

class HTTP_410_GONE(HTTP_4XX_EXCEPTION):
    code = 410

class HTTP_413_PAYLOAD_TOO_LARGE(HTTP_4XX_EXCEPTION):
    code = 413

class HTTP_415_UNSUPPORTED_MEDIA_TYPE(HTTP_4XX_EXCEPTION):
    code = 415

class HTTP_429_TOO_MANY_REQUESTS(HTTP_4XX_EXCEPTION):
    code = 429


# 5xx Codes


class HTTP_500_INTERNAL_SERVER_ERROR(HTTP_5XX_EXCEPTION):
    code = 500

class HTTP_501_NOT_IMPLEMENTED(HTTP_5XX_EXCEPTION):
    code = 501

class HTTP_502_BAD_GATEWAY(HTTP_5XX_EXCEPTION):
    code = 502

class HTTP_503_SERVICE_UNAVAILABLE(HTTP_5XX_EXCEPTION):
    code = 503

class HTTP_504_GATEWAY_TIMEOUT(HTTP_5XX_EXCEPTION):
    code = 504

class HTTP_505_HTTP_VERSION_NOT_SUPPORTED(HTTP_5XX_EXCEPTION):
    code = 505

class HTTP_507_INSUFFICIENT_STORAGE(HTTP_5XX_EXCEPTION):
    code = 507

class HTTP_511_NETWORK_AUTHENTICATION_REQUIRED(HTTP_5XX_EXCEPTION):
    code = 511


# What a client sees of an unexpected error. A database error carries the statement and
# (without hide_parameters) the values of a credential column; the exception is logged
# server side, the client only learns that something failed.
INTERNAL_ERROR = 'Internal server error'

# Exceptions of these libraries describe the database, the broker or their connection
# strings: their text is never stored or shown
_OPAQUE_LIBRARIES = ('sqlalchemy', 'psycopg', 'psycopg2', 'kombu', 'amqp', 'redis', 'celery', 'billiard')


def public_error(e):
    """Message of an unexpected error for the API: rclone's own text, otherwise a generic one"""
    from .utils.abstract_connection import RcloneException
    return str(e) if isinstance(e, RcloneException) else INTERNAL_ERROR


def error_text(e):
    """
    Text of an error for a job's error field, a notification or the Celery task result:
    str(e), except for database and broker errors, which only say what failed
    """
    if type(e).__module__.split('.')[0] in _OPAQUE_LIBRARIES:
        return 'Database or broker error ({}); see the server log'.format(type(e).__name__)
    return str(e)
