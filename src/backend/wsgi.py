import os

from werkzeug.middleware.proxy_fix import ProxyFix

from api import create_app, db
from api.models import * # To ensure that all models are tracked


application = create_app(os.getenv('PYTHON_ENVIRONMENT') or 'dev')

# Traefik terminates TLS and replaces any client-sent X-Forwarded-* headers. Trust
# only the scheme (absolute URLs such as the Swagger spec URL must be https) and the
# client address. Not the host or port: Traefik passes the Host header through, and
# internal_views relies on SERVER_PORT being the port of the accepting socket.
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

if __name__ == '__main__':
    application.run(host='0.0.0.0')
