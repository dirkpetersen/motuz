"""
Serves the built React frontend (`npm run build`, FRONTEND_DIR) in production.

Traefik terminates TLS and routes everything except /internal to uWSGI, but it
cannot serve files. Werkzeug's send_file uses uWSGI's wsgi.file_wrapper (sendfile),
so the Python side only picks the file and the cache headers:
- /js and /css have content hashes in their names and are cached for 30 days,
  /img for 7 days
- index.html is never stored, so a deploy is picked up on the next page load
- any other path gets index.html for react-router (e.g. /clouds), except the
  backend prefixes, which keep returning a real 404

In development webpack-dev-server serves the frontend and FRONTEND_DIR usually does
not exist, so these routes return 404.
"""
import os

from flask import Blueprint, abort, current_app, redirect, send_from_directory
from werkzeug.security import safe_join


# Cache lifetime (seconds) of the top-level asset folders of the build
ASSET_MAX_AGE = {
    'js': 30 * 24 * 3600,
    'css': 30 * 24 * 3600,
    'img': 7 * 24 * 3600,
}

# Paths below these are never answered with index.html
BACKEND_PREFIXES = ('api', 'swaggerui', 'internal')

bp = Blueprint('frontend', __name__)


@bp.route('/<any(js, css, img):folder>/<path:filename>')
def asset(folder, filename):
    root = os.path.join(current_app.config['FRONTEND_DIR'], folder)
    # public, max-age=<ASSET_MAX_AGE>; 404 if missing, safe_join refuses traversal
    return send_from_directory(root, filename, max_age=ASSET_MAX_AGE[folder])


@bp.route('/', defaults={'path': ''})
@bp.route('/<path:path>')
def index(path):
    if path == 'api':
        return redirect('/api/', 308) # Swagger UI, as before this route existed
    if path.split('/', 1)[0] in BACKEND_PREFIXES:
        abort(404)
    root = current_app.config['FRONTEND_DIR']
    # Like the former nginx `try_files $uri /index.html`: files at the root of the build
    # (index.html, favicon, ...) are served as is, everything else is an SPA route
    filename = path if path and os.path.isfile(safe_join(root, path) or '') else 'index.html'
    response = send_from_directory(root, filename)
    response.headers['Cache-Control'] = 'no-store'
    return response
