import React from 'react';
import Toggle from 'react-toggle'

import UriResource from 'components/UriResource.jsx'
import { describeSchedule, formatLocalDateTime, timeZoneName } from 'utils/copySchedule.js'


// How often the table refetches (next run, last job), ms
const REFRESH_DELAY = 30 * 1000;


/**
 * The "Schedules" tab next to the job tables: the user's scheduled repeating copies
 * (README "Scheduled copies") with their times in the browser's time zone, the last
 * job, pause/resume and delete
 */
class CopyScheduleTable extends React.Component {
    constructor(props) {
        super(props);
        this.interval = null;
    }

    render() {
        const offset = new Date().getTimezoneOffset();
        const zone = timeZoneName(offset);
        const cloudTypes = {0: 'file'};
        this.props.connections.forEach(connection => {
            cloudTypes[connection.id] = connection.type;
        });
        const type = id => cloudTypes[id || 0] || '(unknown)';

        const rows = this.props.schedules.map(schedule => (
            <tr key={schedule.id} className='copy-schedule-row'>
                <td className='align-middle'>{schedule.id}</td>
                <td className='align-middle'>{schedule.description}</td>
                <td className='align-middle'>
                    <UriResource protocol={type(schedule.src_cloud_id)} path={schedule.src_resource_path} />
                </td>
                <td className='align-middle'>
                    <UriResource protocol={type(schedule.dst_cloud_id)} path={schedule.dst_resource_path} />
                </td>
                <td className='align-middle' title={schedule.schedule_text || ''}>
                    {describeSchedule(schedule, offset)}
                </td>
                <td className='align-middle copy-schedule-next' title={schedule.next_run_at || ''}>
                    {schedule.enabled ? formatLocalDateTime(schedule.next_run_at) : 'paused'}
                </td>
                <td className='align-middle' title={schedule.last_status || ''}>
                    {schedule.last_job_id ? `#${schedule.last_job_id}` : '-'}
                    {schedule.skipped_runs ? (
                        <div className='small text-muted'>{schedule.skipped_runs} skipped</div>
                    ) : null}
                    {schedule.last_status && !schedule.last_status.startsWith('Started') ? (
                        <div className='small text-warning copy-schedule-status'>{schedule.last_status}</div>
                    ) : null}
                </td>
                <td className='align-middle'>
                    <Toggle
                        aria-label={schedule.enabled ? 'Pause the schedule' : 'Resume the schedule'}
                        checked={schedule.enabled}
                        onChange={event => this.props.onToggle(schedule.id, event.target.checked)}
                    />
                </td>
                <td className='align-middle'>
                    <button
                        type='button'
                        className='btn btn-sm btn-outline-danger'
                        onClick={() => this.handleDelete(schedule)}
                    >
                        Delete
                    </button>
                </td>
            </tr>
        ));

        return (
            <table className='table table-sm table-striped table-hover text-start copy-schedule-table'>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Description</th>
                        <th>Source</th>
                        <th>Destination</th>
                        <th>Repeats</th>
                        <th title={`Times in your time zone (${zone})`}>Next run ({zone})</th>
                        <th>Last job</th>
                        <th>Enabled</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>
                    {rows.length ? rows : (
                        <tr>
                            <td colSpan={9} className='text-muted'>
                                No schedules. Turn on "Scheduled repeat" in the New Copy Job dialog to repeat a copy.
                            </td>
                        </tr>
                    )}
                </tbody>
            </table>
        );
    }

    handleDelete(schedule) {
        if (confirm(`Delete schedule ${schedule.id}? Its copy jobs stay.`)) {
            this.props.onDelete(schedule.id);
        }
    }

    componentDidMount() {
        this.props.fetchData();
        this.interval = window.setInterval(() => this.props.fetchData(), REFRESH_DELAY);
    }

    componentWillUnmount() {
        if (this.interval) {
            window.clearInterval(this.interval);
            this.interval = null;
        }
    }
}

CopyScheduleTable.defaultProps = {
    schedules: [],
    connections: [],
    fetchData: () => {},
    onToggle: (id, enabled) => {},
    onDelete: (id) => {},
}

import {connect} from 'react-redux';
import { deleteCopySchedule, listCopySchedules, updateCopySchedule } from 'actions/apiActions.jsx'

const mapStateToProps = state => ({
    schedules: state.api.schedules,
    connections: state.api.clouds,
});

const mapDispatchToProps = dispatch => ({
    fetchData: () => dispatch(listCopySchedules()),
    onToggle: (id, enabled) => dispatch(updateCopySchedule(id, {enabled})),
    onDelete: id => dispatch(deleteCopySchedule(id)),
});

export default connect(mapStateToProps, mapDispatchToProps)(CopyScheduleTable);
