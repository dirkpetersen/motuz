// Run with: node --test test/frontend/   (from the repository root, needs node_modules)
import test from 'node:test';
import assert from 'node:assert/strict';

import {parentDirectory} from '../../src/frontend/js/utils/parentDirectory.js';

const LOCAL = {id: 0, type: 'file'};
const S3 = {id: 3, type: 's3', bucket: 'mybucket'};
const AZURE = {id: 4, type: 'azureblob'};
const GCS = {id: 5, type: 'google cloud storage'};
const SFTP = {id: 6, type: 'sftp'};
const ONEDRIVE = {id: 7, type: 'onedrive'};

test('local paths', () => {
    assert.equal(parentDirectory('/home/user/data', LOCAL), '/home/user');
    assert.equal(parentDirectory('/home', LOCAL), '/');
    assert.equal(parentDirectory('/home/user/', LOCAL), '/home');
    assert.equal(parentDirectory('/', LOCAL), null);
    assert.equal(parentDirectory('', LOCAL), null);
});

test('bucket types stop at the bucket root', () => {
    assert.equal(parentDirectory('/mybucket/a/b', S3), '/mybucket/a');
    assert.equal(parentDirectory('/mybucket/a', S3), '/mybucket');
    assert.equal(parentDirectory('/mybucket', S3), null);
    assert.equal(parentDirectory('/mybucket/', S3), null);
    assert.equal(parentDirectory('/', S3), null);
    assert.equal(parentDirectory('/container', AZURE), null);
    assert.equal(parentDirectory('/container/dir', AZURE), '/container');
});

test('Google Cloud Storage paths have no leading slash', () => {
    assert.equal(parentDirectory('bucket/a/b', GCS), 'bucket/a');
    assert.equal(parentDirectory('bucket/a', GCS), 'bucket');
    assert.equal(parentDirectory('bucket', GCS), null);
    assert.equal(parentDirectory('', GCS), null);
});

test('non-bucket remotes can copy into their root', () => {
    assert.equal(parentDirectory('/Documents', ONEDRIVE), '/');
    assert.equal(parentDirectory('/srv/data', SFTP), '/srv');
    assert.equal(parentDirectory('/', SFTP), null);
});

test('bad input', () => {
    assert.equal(parentDirectory(undefined, LOCAL), null);
    assert.equal(parentDirectory(null), null);
    assert.equal(parentDirectory('/a/b'), '/a');
});
