// Where a copy or integrity-check job runs, from the job's `pool` and `pool_status`
// (managers/worker_manager.annotate_location): the Motuz server itself ('central'), or a
// pool of remote workers, e.g. "aws · running on c7gn.2xlarge" for a temporary EC2 worker.

export const CENTRAL = 'central';

/** "aws · starting worker (c7gn.large)", "onprem", or null for jobs on the Motuz server */
export function describeJobLocation(job) {
    const pool = job && typeof job.pool === 'string' ? job.pool : null;
    if (!pool || pool === CENTRAL) {
        return null;
    }
    const status = typeof job.pool_status === 'string' ? job.pool_status.trim() : '';
    return status ? `${pool} · ${status}` : pool;
}

/** The "Runs on" line of the job dialogs */
export function jobLocationDetail(job) {
    return describeJobLocation(job) || 'Motuz server';
}
