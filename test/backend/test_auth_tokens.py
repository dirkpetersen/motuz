"""
Login sessions, refresh token rotation and logout (managers/auth_manager.py), against
an in-memory SQLite revoked_token table.
"""
import time
import unittest
from unittest import mock

from flask import Flask
import flask_jwt_extended as flask_jwt

from api.application import db, jwt
from api.config import Config
from api.exceptions import HTTP_401_UNAUTHORIZED
from api.managers import auth_manager
from api.models import RevokedToken


class AuthTokensTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = Flask('auth-tokens-test')
        cls.app.config.update(
            JWT_SECRET_KEY='test-secret-of-at-least-32-bytes-for-hs256',
            JWT_IDENTITY_CLAIM=Config.JWT_IDENTITY_CLAIM,
            JWT_ACCESS_TOKEN_EXPIRES=Config.JWT_ACCESS_TOKEN_EXPIRES,
            JWT_REFRESH_TOKEN_EXPIRES=Config.JWT_REFRESH_TOKEN_EXPIRES,
            SQLALCHEMY_DATABASE_URI='sqlite://',
        )
        db.init_app(cls.app)
        jwt.init_app(cls.app) # the same manager, so the same blocklist loader

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        RevokedToken.__table__.create(db.engine)
        pam = mock.MagicMock()
        pam.return_value.code = 0
        patcher = mock.patch.object(auth_manager, 'pam', pam)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        db.session.remove()
        RevokedToken.__table__.drop(db.engine)
        self.context.pop()

    def login(self):
        with self.app.test_request_context():
            return auth_manager.login_user({'username': 'alice', 'password': 'pw'})

    def call(self, function, token):
        with self.app.test_request_context(headers={'Authorization': 'Bearer ' + token}):
            return function()

    def user(self, access):
        return self.call(auth_manager.get_logged_in_user, access)

    def refresh(self, refresh):
        return self.call(auth_manager.refresh_token, refresh)

    def logout(self, refresh):
        return self.call(auth_manager.logout_user, refresh)

    def claims(self, token):
        return flask_jwt.decode_token(token, allow_expired=True)

    def test_login(self):
        tokens = self.login()
        access, refresh = self.claims(tokens['access']), self.claims(tokens['refresh'])
        self.assertEqual(access['identity'], 'alice') # frontend contract (JWT_IDENTITY_CLAIM)
        self.assertEqual(access['sid'], refresh['sid'])
        self.assertEqual(access['exp'] - access['iat'], 15 * 60)
        self.assertEqual(refresh['exp'] - refresh['iat'], 30 * 24 * 3600)
        self.assertEqual(self.user(tokens['access']), 'alice')
        self.assertNotEqual(self.login()['access'], tokens['access'])

    def test_logout_revokes_access_and_refresh_tokens(self):
        tokens = self.login()
        refreshed = self.refresh(tokens['refresh'])
        other = self.login() # another login (browser) of the same user
        self.logout(refreshed['refresh'])
        for access in (tokens['access'], refreshed['access']):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                self.user(access)
        for refresh in (tokens['refresh'], refreshed['refresh']):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                self.refresh(refresh)
        self.assertEqual(self.user(other['access']), 'alice')

    def test_logout_needs_the_refresh_token(self):
        tokens = self.login()
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            self.logout(tokens['access'])

    def test_refresh_rotates(self):
        tokens = self.login()
        refreshed = self.refresh(tokens['refresh'])
        self.assertEqual(self.claims(refreshed['refresh'])['sid'], self.claims(tokens['refresh'])['sid'])
        self.assertEqual(self.user(refreshed['access']), 'alice')
        self.assertEqual(self.user(tokens['access']), 'alice') # access tokens are not rotated
        row = RevokedToken.query.filter_by(jti=self.claims(tokens['refresh'])['jti']).one()
        self.assertEqual(row.type, 'refresh')
        self.assertAlmostEqual(row.grace_until, time.time() + auth_manager.REFRESH_GRACE_SECONDS, delta=5)

    def test_concurrent_refresh_within_grace_window(self):
        tokens = self.login()
        first = self.refresh(tokens['refresh'])
        second = self.refresh(tokens['refresh']) # another tab, same moment
        self.assertEqual(self.user(first['access']), 'alice')
        self.assertEqual(self.user(second['access']), 'alice')
        self.assertEqual(RevokedToken.query.count(), 1)

    def test_reuse_after_grace_window_revokes_the_session(self):
        tokens = self.login()
        refreshed = self.refresh(tokens['refresh'])
        later = time.time() + auth_manager.REFRESH_GRACE_SECONDS + 1
        with mock.patch.object(auth_manager.time, 'time', return_value=later):
            with self.assertRaises(HTTP_401_UNAUTHORIZED):
                self.refresh(tokens['refresh'])
        # The session is gone, including the tokens of the legitimate client
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            self.user(refreshed['access'])
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            self.refresh(refreshed['refresh'])

    def test_logout_with_a_just_rotated_token_ends_its_grace(self):
        tokens = self.login()
        self.refresh(tokens['refresh'])
        self.logout(tokens['refresh'])
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            self.refresh(tokens['refresh'])

    def test_tokens_without_session(self):
        # Issued before sessions existed: still valid, refresh starts a session
        with self.app.test_request_context():
            access = flask_jwt.create_access_token(identity='alice')
            refresh = flask_jwt.create_refresh_token(identity='alice')
        self.assertEqual(self.user(access), 'alice')
        refreshed = self.refresh(refresh)
        self.assertTrue(self.claims(refreshed['access'])['sid'])
        self.logout(refreshed['refresh'])
        with self.assertRaises(HTTP_401_UNAUTHORIZED):
            self.user(refreshed['access'])

    def test_expired_rows_are_cleaned(self):
        tokens = self.login()
        claims = self.claims(tokens['refresh'])
        db.session.add(RevokedToken(jti='old', type='refresh', identity='alice', exp=int(time.time()) - 10))
        db.session.commit()
        self.logout(tokens['refresh'])
        self.assertEqual({row.jti for row in RevokedToken.query.all()}, {claims['jti'], claims['sid']})


if __name__ == '__main__':
    unittest.main()
