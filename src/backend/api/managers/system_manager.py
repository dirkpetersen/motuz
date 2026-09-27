import datetime
import json
import logging
import os
import re
import pwd
import subprocess

from flask import request

from ..exceptions import *
from ..managers.auth_manager import token_required, get_logged_in_user
from ..application import db
from ..managers import cloud_connection_manager
from ..utils.rclone_connection import RcloneConnection
from ..utils.local_connection import LocalConnection
from ..utils.abstract_connection import RcloneException
from ..utils import file_view


@token_required
def get_uid():
    uid = pwd.getpwnam(get_logged_in_user(request)).pw_uid
    return {
        "uid": uid,
    }


# no token_required
def get_info():
    payload = {}

    try:
        payload['rclone_version'] = (subprocess
            .check_output("rclone --version", shell=True)
            .decode('utf-8')
            .strip()
            .replace('\n', ' | ')
            .replace('- ', '')
        )
    except:
        payload['rclone_version'] = "ERROR"

    try:
        payload['date'] = str(datetime.datetime.now())
    except:
        payload['date'] = "ERROR"

    try:
        payload['database_host'] = db.engine.url.host
    except:
        payload['database_host'] = 'ERROR'

    try:
        payload['status'] = 'healthy' if all([
            payload[key] != 'ERROR' for key in payload
        ]) else 'unhealthy'
    except:
        payload['status'] = 'unhealthy'

    return payload



@token_required
def ls(data):
    user = get_logged_in_user(request)
    path = data['path']
    connection_id = data['connection_id']

    if connection_id == 0:
        cloud_connection = Dummy()
        cloud_connection.owner = user
        connection = LocalConnection()
    else:
        cloud_connection = cloud_connection_manager.retrieve(connection_id)
        if cloud_connection.owner != user:
            # Should never happen
            raise HTTP_404_NOT_FOUND('Cloud Connection with id {} not found'.format(connection_id))

        connection = RcloneConnection()

    try:
        return connection.ls(data=cloud_connection, path=path)
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST(str(e))


@token_required
def lshome():
    user = get_logged_in_user(request)

    cloud_connection = Dummy()
    cloud_connection.owner = user
    connection = LocalConnection()

    try:
        return connection.lshome(data=cloud_connection)
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST(str(e))



@token_required
def view(data):
    """
    The first 1 MiB of a text file, read as the logged-in user, for the read-only
    viewer: {path, content, truncated, size, encoding}. Contents are never logged.
    """
    cloud_connection, connection = _view_connection(data['connection_id'])
    try:
        return connection.view(data=cloud_connection, path=data['path'])
    except file_view.ViewError as e:
        raise _VIEW_EXCEPTIONS.get(e.status, HTTP_400_BAD_REQUEST)(str(e))
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST(str(e))


@token_required
def view_chunk(data):
    """
    One chunk of a text file for the pager, read as the logged-in user:
    {path, content, offset, end, size, bof, eof, encoding}. Parameters: `offset` (a
    forward read), `before` (the chunk that ends there) or `from_end` (the last
    chunk), `length` (at most file_view.CHUNK_BYTES) and `follow` (the viewer's
    follow mode, see file_view.ChunkRequest). Contents are never logged.
    """
    try:
        request_ = file_view.parse_chunk_request(data)
    except file_view.ViewError as e:
        raise HTTP_400_BAD_REQUEST(str(e))
    cloud_connection, connection = _view_connection(data['connection_id'])
    try:
        return connection.view_chunk(data=cloud_connection, path=data['path'], request=request_)
    except file_view.ViewError as e:
        raise _VIEW_EXCEPTIONS.get(e.status, HTTP_400_BAD_REQUEST)(str(e))
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST(str(e))


def _view_connection(connection_id):
    """(connection row, connection) for viewing as the logged-in user; 404 for others' connections"""
    user = get_logged_in_user(request)
    if connection_id == 0:
        cloud_connection = Dummy()
        cloud_connection.owner = user
        return cloud_connection, LocalConnection()

    cloud_connection = cloud_connection_manager.retrieve(connection_id) # 404 for others' connections
    if cloud_connection.owner != user:
        # Should never happen
        raise HTTP_404_NOT_FOUND('Cloud Connection with id {} not found'.format(connection_id))
    return cloud_connection, RcloneConnection()


_VIEW_EXCEPTIONS = {
    400: HTTP_400_BAD_REQUEST,
    403: HTTP_403_FORBIDDEN,
    404: HTTP_404_NOT_FOUND,
    415: HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    504: HTTP_504_GATEWAY_TIMEOUT,
}


@token_required
def mkdir(data):
    user = get_logged_in_user(request)
    path = data['path']
    connection_id = data['connection_id']

    if connection_id == 0:
        cloud_connection = Dummy()
        cloud_connection.owner = user
        connection = LocalConnection()
    else:
        cloud_connection = cloud_connection_manager.retrieve(connection_id)
        if cloud_connection.owner != user:
            # Should never happen
            raise HTTP_404_NOT_FOUND('Cloud Connection with id {} not found'.format(connection_id))

        connection = RcloneConnection()

    try:
        return connection.mkdir(data=cloud_connection, path=path)
    except RcloneException as e:
        raise HTTP_400_BAD_REQUEST(str(e))



class Dummy:
    pass
