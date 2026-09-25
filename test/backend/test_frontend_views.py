import os
import tempfile
import unittest
from unittest import mock

from api import create_app


class FrontendTestCase(unittest.TestCase):
    """Serving the built frontend and keeping /internal off the proxied socket."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        for folder in ('js', 'img'):
            os.makedirs(os.path.join(root, folder))
        files = {
            'index.html': '<html><script src="/js/app-abc.bundle.js"></script></html>',
            'favicon.ico': 'ico',
            'js/app-abc.bundle.js': 'console.log(1)',
            'img/logo.png': 'png',
        }
        for name, content in files.items():
            with open(os.path.join(root, name), 'w') as f:
                f.write(content)
        # SERVER_PORT is the accepting socket's port under uWSGI and the dev server
        cls.proxied = {'SERVER_PORT': '5000'}
        cls.broker = {'SERVER_PORT': '5001'}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.app = create_app('test')
        self.old_frontend_dir = self.app.config['FRONTEND_DIR']
        self.app.config['FRONTEND_DIR'] = self.tmp.name
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config['FRONTEND_DIR'] = self.old_frontend_dir

    def get(self, path):
        response = self.client.get(path, environ_overrides=self.proxied)
        response.get_data()
        response.close() # send_file responses hold the file open
        return response

    def test_index_is_never_stored(self):
        response = self.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'app-abc.bundle.js', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_spa_routes_get_index(self):
        for path in ('/clouds', '/some/deep/route'):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(b'app-abc.bundle.js', response.data)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_root_files_served_as_is(self):
        response = self.get('/favicon.ico')
        self.assertEqual(response.data, b'ico')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_assets_are_cached(self):
        response = self.get('/js/app-abc.bundle.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'console.log(1)')
        self.assertEqual(response.headers['Cache-Control'], 'public, max-age=2592000')
        self.assertIn('javascript', response.headers['Content-Type'])
        response = self.get('/img/logo.png')
        self.assertEqual(response.headers['Cache-Control'], 'public, max-age=604800')

    def test_missing_asset_is_404_not_index(self):
        self.assertEqual(self.get('/js/missing.js').status_code, 404)
        self.assertEqual(self.get('/js/..%2Findex.html').status_code, 404)

    def test_backend_prefixes_are_not_the_spa(self):
        for path in ('/api/does-not-exist', '/swaggerui/nope.js', '/internal/oauth/token', '/internal/other'):
            self.assertEqual(self.get(path).status_code, 404, path)
        response = self.get('/api')
        self.assertEqual(response.status_code, 308)
        self.assertTrue(response.headers['Location'].endswith('/api/'))

    def test_no_frontend_build(self):
        self.app.config['FRONTEND_DIR'] = os.path.join(self.tmp.name, 'missing')
        self.assertEqual(self.get('/').status_code, 404)
        self.assertEqual(self.get('/js/app-abc.bundle.js').status_code, 404)

    @mock.patch('api.managers.token_broker_manager.handle_token_request', return_value=(200, {'access_token': 'A'}))
    def test_broker_only_on_broker_socket(self, handle):
        form = {'grant_type': 'refresh_token', 'refresh_token': 'handle'}
        response = self.client.post('/internal/oauth/token', data=form, environ_overrides=self.proxied)
        self.assertEqual(response.status_code, 404)
        handle.assert_not_called()

        response = self.client.post('/internal/oauth/token', data=form, environ_overrides=self.broker)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'access_token': 'A'})
        handle.assert_called_once()

    @mock.patch('api.managers.token_broker_manager.handle_token_request', return_value=(200, {'access_token': 'A'}))
    def test_forwarded_headers_cannot_reach_broker(self, handle):
        """wsgi.py trusts X-Forwarded-Proto/-For only: no header can change SERVER_PORT."""
        os.environ.setdefault('PYTHON_ENVIRONMENT', 'test')
        import wsgi
        self.app.config['FRONTEND_DIR'] = self.tmp.name
        client = wsgi.application.test_client()
        headers = {'Host': 'localhost:5001', 'X-Forwarded-Host': 'localhost:5001', 'X-Forwarded-Port': '5001'}
        response = client.post('/internal/oauth/token', data={'grant_type': 'refresh_token'},
                               headers=headers, environ_overrides=self.proxied)
        self.assertEqual(response.status_code, 404)
        handle.assert_not_called()

        # The scheme comes from Traefik, so absolute URLs (trailing-slash redirects) stay https
        response = client.get('/api/connections', headers={'X-Forwarded-Proto': 'https'}, environ_overrides=self.proxied)
        self.assertEqual(response.status_code, 308)
        self.assertEqual(response.headers['Location'], 'https://localhost/api/connections/')


if __name__ == '__main__':
    unittest.main()
