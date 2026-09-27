// Run with: node --test test/frontend/   (from the repository root)
import test from 'node:test';
import assert from 'node:assert/strict';

import {describeJobLocation, jobLocationDetail} from '../../src/frontend/js/utils/jobLocation.js';

test('jobs on the Motuz server have no location line', () => {
    assert.equal(describeJobLocation({pool: 'central', pool_status: null}), null);
    assert.equal(describeJobLocation({}), null); // older servers: no pool
    assert.equal(describeJobLocation(null), null);
    assert.equal(jobLocationDetail({pool: 'central'}), 'Motuz server');
});

test('remote pools with and without a status', () => {
    assert.equal(describeJobLocation({pool: 'onprem', pool_status: null}), 'onprem');
    assert.equal(describeJobLocation({pool: 'onprem', pool_status: 'running on proxmox-1'}), 'onprem · running on proxmox-1');
    assert.equal(describeJobLocation({pool: 'aws', pool_status: 'starting worker (c7gn.large)'}),
                 'aws · starting worker (c7gn.large)');
    assert.equal(jobLocationDetail({pool: 'aws', pool_status: 'running on c7gn.2xlarge'}), 'aws · running on c7gn.2xlarge');
    assert.equal(describeJobLocation({pool: 'aws', pool_status: '  '}), 'aws');
});
