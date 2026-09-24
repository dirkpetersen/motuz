import json
import uuid
import datetime

from flask import request

from ..application import db
from ..models import CloudConnection
from ..exceptions import *
from ..utils.rclone_connection import RcloneConnection
from ..managers.auth_manager import token_required, get_logged_in_user


# Columns a client may set. Everything else (id, owner, created_at) is server controlled.
_WRITABLE_FIELDS = frozenset(
    column.name for column in CloudConnection.__table__.columns
    if column.name not in ('id', 'owner', 'created_at')
)


# Write-only fields (PrivateString in the API). The API never returns them, so an
# edit form submits them empty; an empty value on update means "keep the stored one".
_SECRET_FIELDS = frozenset((
    's3_secret_access_key',
    's3_session_token',
    'azure_key',
    'azure_sas_url',
    'swift_key',
    'gcp_service_account_credentials',
    'sftp_pass',
    'dropbox_token',
    'onedrive_token',
    'webdav_pass',
))


def _writable(data):
    return {key: value for key, value in data.items() if key in _WRITABLE_FIELDS}


@token_required
def list():
    owner = get_logged_in_user(request)

    cloud_connections = (CloudConnection.query
        .filter_by(owner=owner)
        .order_by(CloudConnection.id.asc())
        .all()
    )
    return cloud_connections


@token_required
def create(data):
    owner = get_logged_in_user(request)

    cloud_connection = CloudConnection(**_writable(data))
    cloud_connection.owner = owner

    db.session.add(cloud_connection)
    db.session.commit()

    return cloud_connection



@token_required
def retrieve(id):
    cloud_connection = db.session.get(CloudConnection, id)

    if cloud_connection is None:
        raise HTTP_404_NOT_FOUND('Cloud Connection with id {} not found'.format(id))

    owner = get_logged_in_user(request)

    if cloud_connection.owner != owner:
        raise HTTP_404_NOT_FOUND('Cloud Connection with id {} not found'.format(id))

    return cloud_connection


def owned_cloud_id(cloud_id):
    """
    Returns the id of a connection owned by the logged in user, or None for the
    local filesystem (None or 0). Raises 404 for connections of other users.
    """
    if not cloud_id:
        return None
    return retrieve(cloud_id).id


@token_required
def update(id, data):
    cloud_connection = retrieve(id)

    for key, value in _writable(data).items():
        if key in _SECRET_FIELDS and not value:
            continue
        setattr(cloud_connection, key, value)

    db.session.commit()
    return cloud_connection


@token_required
def delete(id):
    cloud_connection = retrieve(id)

    db.session.delete(cloud_connection)
    db.session.commit()

    return cloud_connection



@token_required
def verify(data):
    owner = get_logged_in_user(request)
    cloud_connection = CloudConnection(**_writable(data))
    cloud_connection.owner = owner

    rclone = RcloneConnection()
    return rclone.verify(cloud_connection)
