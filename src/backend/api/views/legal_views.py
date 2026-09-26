"""
Public privacy policy (/privacy) and terms of service (/terms).

Server-side HTML without JavaScript and without login, so that crawlers and reviewers
(e.g. Google's OAuth app verification) can read them. They do not depend on the
frontend build, so they also work with the development server. Who runs the
installation comes from MOTUZ_OPERATOR_NAME, MOTUZ_CONTACT_EMAIL and MOTUZ_OPERATOR_URL
(config.py); Jinja escapes every value.
"""
import re

from flask import Blueprint, current_app, render_template


# Date of the current wording of both pages; change it whenever the text changes
EFFECTIVE_DATE = '2026-09-25'

# Top-level paths served here; frontend_views never answers them with the SPA
LEGAL_PAGES = ('privacy', 'terms')

# Legal text changes only with a deploy or a configuration change
CACHE_CONTROL = 'public, max-age=3600'

# The pages have no scripts, no forms and no external resources
CONTENT_SECURITY_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_EMAIL = re.compile(r'^[^@\s<>"\'(),;:\\]+@[^@\s<>"\'(),;:\\]+\.[^@\s<>"\'(),;:\\]+$')
_URL = re.compile(r'^https?://[^\s<>"\']+$', re.IGNORECASE)

bp = Blueprint('legal', __name__)


def operator_info(config):
    """
    The operator as the templates use it. Values are plain text (escaped by Jinja);
    the email is only linked when it looks like an address and the URL only when it
    is http(s), so no configuration can produce a javascript: link.
    """
    email = config.get('CONTACT_EMAIL')
    url = config.get('OPERATOR_URL')
    return {
        'name': config.get('OPERATOR_NAME'),
        'email': email,
        'email_is_address': bool(email and _EMAIL.match(email)),
        'url': url if url and _URL.match(url) else None,
    }


def _render(template):
    response = current_app.make_response(render_template(
        template,
        operator=operator_info(current_app.config),
        effective_date=EFFECTIVE_DATE,
    ))
    response.headers['Cache-Control'] = CACHE_CONTROL
    response.headers['Content-Security-Policy'] = CONTENT_SECURITY_POLICY
    return response


@bp.route('/privacy')
def privacy():
    return _render('legal/privacy.html')


@bp.route('/terms')
def terms():
    return _render('legal/terms.html')
