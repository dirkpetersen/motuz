/**
 * Returns an array of jobs that are currently in progress and write to `dst`.
 * Returns an empty array if no such jobs exist.
 *
 * @param data: dict{
 *   id: int
 *   dst_cloud_id: int
 *   dst_resource_path: str
 * }
 */
export function getJobsInProgressForDestination(state, data) {
    const {dst_cloud_id, dst_resource_path} = data
    // Local is sent as a missing id but comes back from the server as null
    return state.jobs.filter(d => (
        d.progress_state === "PROGRESS" &&
        (d.dst_cloud_id || 0) === (dst_cloud_id || 0) &&
        d.dst_resource_path === dst_resource_path
    ))
}
