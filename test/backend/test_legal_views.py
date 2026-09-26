import importlib
import os
import tempfile
import unittest
from unittest import mock

from api import create_app
from api.views import legal_views


class LegalPagesTestCase(unittest.TestCase):
    """Public /privacy and /terms: no login, plain HTML, operator from the configuration."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(cls.tmp.name, 'index.html'), 'w') as f:
            f.write('<html><script src="/js/app-abc.bundle.js"></script></html>')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.app = create_app('test')
        self.saved = {key: self.app.config.get(key)
                      for key in ('FRONTEND_DIR', 'OPERATOR_NAME', 'CONTACT_EMAIL', 'OPERATOR_URL')}
        self.app.config['FRONTEND_DIR'] = self.tmp.name
        self.configure()
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.update(self.saved)

    def configure(self, name=None, email=None, url=None):
        self.app.config.update(OPERATOR_NAME=name, CONTACT_EMAIL=email, OPERATOR_URL=url)

    def get(self, path):
        response = self.client.get(path, environ_overrides={'SERVER_PORT': '5000'})
        response.get_data()
        response.close()
        return response

    def test_pages_are_public_html(self):
        for path, heading in (('/privacy', 'Privacy Policy'), ('/terms', 'Terms of Service')):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.mimetype, 'text/html')
            text = response.get_data(as_text=True)
            self.assertTrue(text.startswith('<!DOCTYPE html>'), path)
            self.assertIn('<h1>{}</h1>'.format(heading), text)
            self.assertIn('Effective date: ' + legal_views.EFFECTIVE_DATE, text)
            self.assertNotIn('<script', text)
            self.assertNotIn('app-abc.bundle.js', text) # not the SPA
            # Links between the pages and to the home page
            for link in ('href="/"', 'href="/privacy"', 'href="/terms"'):
                self.assertIn(link, text, path)
            self.assertEqual(response.headers['Cache-Control'], legal_views.CACHE_CONTROL)
            self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_head_request(self):
        response = self.client.head('/privacy')
        self.assertEqual(response.status_code, 200)

    def test_privacy_content(self):
        text = self.get('/privacy').get_data(as_text=True)
        for needle in (
            'https://www.googleapis.com/auth/drive',
            'https://developers.google.com/terms/api-services-user-data-policy',
            'Limited Use requirements',
            'https://myaccount.google.com/permissions',
            'Microsoft',
            'Files.ReadWrite.All',
            'login name',
            'IP address',
            'local storage',
            'not used to develop, improve or train generalized artificial intelligence',
            'deletes the stored tokens',
        ):
            self.assertIn(needle, text)

    def test_terms_content(self):
        text = self.get('/terms').get_data(as_text=True)
        for needle in ('Acceptable use', 'No warranty', 'Limitation of liability', 'Availability',
                       'Suspension and termination', 'Changes to these terms', 'href="/privacy"'):
            self.assertIn(needle, text)

    def test_defaults_without_operator(self):
        for path in ('/privacy', '/terms'):
            text = self.get(path).get_data(as_text=True)
            self.assertIn('contact the administrator of this Motuz installation', text)
            self.assertNotIn('mailto:', text)
            self.assertNotIn('Operator:', text)
            self.assertNotIn('None', text)

    def test_operator_is_shown(self):
        self.configure('Example Institute', 'motuz-admin@example.org', 'https://www.example.org/')
        for path in ('/privacy', '/terms'):
            text = self.get(path).get_data(as_text=True)
            self.assertIn('Operator: <a href="https://www.example.org/">Example Institute</a>', text)
            self.assertIn('<a href="mailto:motuz-admin@example.org">motuz-admin@example.org</a>', text)
            self.assertNotIn('administrator of this Motuz installation', text)

    def test_values_are_escaped(self):
        self.configure('<script>alert(1)</script> & "Co"', 'a"b@x.org', 'https://example.org/?a=1&b=2')
        text = self.get('/privacy').get_data(as_text=True)
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;Co&#34;', text)
        # Not an address: shown as text, not linked
        self.assertNotIn('mailto:', text)
        self.assertIn('a&#34;b@x.org', text)
        # The URL is linked, escaped
        self.assertIn('href="https://example.org/?a=1&amp;b=2"', text)

    def test_only_http_urls_are_linked(self):
        for url in ('javascript:alert(1)', 'data:text/html,x', '//evil.example', 'ftp://example.org'):
            self.configure('Example Institute', None, url)
            text = self.get('/privacy').get_data(as_text=True)
            self.assertNotIn(url, text, url)
            self.assertIn('Operator: Example Institute<br>', text)

    def test_trailing_slash_and_subpaths(self):
        response = self.get('/privacy/')
        self.assertEqual(response.status_code, 308)
        self.assertTrue(response.headers['Location'].endswith('/privacy'))
        response = self.get('/terms/')
        self.assertEqual(response.status_code, 308)
        self.assertTrue(response.headers['Location'].endswith('/terms'))
        for path in ('/privacy/x', '/terms/a/b'):
            self.assertEqual(self.get(path).status_code, 404, path)

    def test_spa_and_backend_routes_unchanged(self):
        for path in ('/', '/clouds', '/login', '/privacyx', '/terms-of-use', '/a/privacy'):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(b'app-abc.bundle.js', response.data, path)
            self.assertEqual(response.headers['Cache-Control'], 'no-store', path)
        for path in ('/api/privacy', '/api/does-not-exist', '/swaggerui/nope.js', '/internal/oauth/token'):
            self.assertEqual(self.get(path).status_code, 404, path)

    def test_pages_do_not_need_the_frontend_build(self):
        self.app.config['FRONTEND_DIR'] = os.path.join(self.tmp.name, 'missing')
        self.assertEqual(self.get('/privacy').status_code, 200)
        self.assertEqual(self.get('/terms').status_code, 200)


class OperatorConfigTestCase(unittest.TestCase):
    """MOTUZ_OPERATOR_* are optional; empty or whitespace means unset."""

    def load(self, env):
        from api import config
        with mock.patch.dict(os.environ, env):
            return importlib.reload(config).Config

    def tearDown(self):
        from api import config
        importlib.reload(config)

    def test_defaults(self):
        env = {'MOTUZ_OPERATOR_NAME': '', 'MOTUZ_CONTACT_EMAIL': '  ', 'MOTUZ_OPERATOR_URL': ''}
        cfg = self.load(env)
        self.assertIsNone(cfg.OPERATOR_NAME)
        self.assertIsNone(cfg.CONTACT_EMAIL)
        self.assertIsNone(cfg.OPERATOR_URL)

    def test_values(self):
        env = {'MOTUZ_OPERATOR_NAME': ' Example Institute ', 'MOTUZ_CONTACT_EMAIL': 'a@example.org',
               'MOTUZ_OPERATOR_URL': 'https://example.org/'}
        cfg = self.load(env)
        self.assertEqual(cfg.OPERATOR_NAME, 'Example Institute')
        self.assertEqual(cfg.CONTACT_EMAIL, 'a@example.org')
        self.assertEqual(cfg.OPERATOR_URL, 'https://example.org/')

    def test_operator_info(self):
        info = legal_views.operator_info({'OPERATOR_NAME': 'X', 'CONTACT_EMAIL': 'a@example.org',
                                          'OPERATOR_URL': 'HTTPS://example.org'})
        self.assertEqual(info, {'name': 'X', 'email': 'a@example.org', 'email_is_address': True,
                                'url': 'HTTPS://example.org'})
        info = legal_views.operator_info({})
        self.assertEqual(info, {'name': None, 'email': None, 'email_is_address': False, 'url': None})
        self.assertFalse(legal_views.operator_info({'CONTACT_EMAIL': 'not an address'})['email_is_address'])


if __name__ == '__main__':
    unittest.main()
