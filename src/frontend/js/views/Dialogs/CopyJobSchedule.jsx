import React from 'react';
import Toggle from 'react-toggle'

import {
    FREQUENCIES, FREQUENCY_LABELS, MAX_DAY_OF_MONTH, WEEKDAYS, defaultNewGb, describeScheduleEgress, timeZoneName,
} from 'utils/copySchedule.js'
import { needsConfirmation } from 'utils/egressCost.js'


/** The initial state of the section (off); weekday = today's, 0 = Monday */
export function initialSchedule(now = new Date()) {
    return {
        enabled: false,
        frequency: 'daily',
        time: '02:00',
        minute: 0,
        weekday: (now.getDay() + 6) % 7,
        dayOfMonth: 1,
        runNow: true,
        newGb: '',
        newGbTouched: false,
        repeatConfirmed: false,
    };
}

/** The expected new data per run in GB: the user's number, else 10% of the measured size */
export function scheduleNewGb(schedule, estimate) {
    if (schedule.newGbTouched) {
        const value = Number(schedule.newGb);
        return schedule.newGb !== '' && Number.isFinite(value) && value >= 0 ? value : null;
    }
    return defaultNewGb(estimate);
}

/** Whether the schedule may be submitted: a schedule out of AWS needs its own confirmation */
export function scheduleReady(schedule, estimate) {
    return !schedule.enabled || !needsConfirmation(estimate) || schedule.repeatConfirmed;
}


function Row({label, children}) {
    return (
        <div className="row mb-3">
            <div className="col-4 text-end">
                <b className='form-label'>{label}</b>
            </div>
            <div className="col-7">{children}</div>
            <div className="col-1"></div>
        </div>
    );
}


/**
 * The "Scheduled repeat" section of the New Copy Job dialog (collapsed, off by
 * default): how often the copy repeats and when, in the browser's time zone (the
 * dialog converts to UTC, utils/copySchedule.js), "run once now", and for a copy out
 * of AWS the monthly cost with a required confirmation. Controlled: `value` is the
 * dialog's state, onChange(changes) updates it.
 */
class CopyJobSchedule extends React.Component {
    render() {
        const {value, estimate} = this.props;
        const zone = timeZoneName(new Date().getTimezoneOffset());
        const set = changes => this.props.onChange(changes);
        const egress = value.enabled ? describeScheduleEgress(estimate, value.frequency, scheduleNewGb(value, estimate)) : null;

        return (
            <details className='copy-job-schedule' open={value.enabled || undefined}>
                <summary className='text-primary h5 mt-5 mb-2'>
                    Scheduled repeat
                </summary>

                <Row label='Repeat this copy'>
                    <Toggle
                        name='schedule_enabled'
                        className='form-label'
                        checked={value.enabled}
                        onChange={event => set({enabled: event.target.checked})}
                    />
                </Row>

                {value.enabled && (
                    <>
                        <Row label='Repeats'>
                            <select
                                name='schedule_frequency'
                                className='form-select'
                                value={value.frequency}
                                onChange={event => set({frequency: event.target.value})}
                            >
                                {FREQUENCIES.map(f => <option key={f} value={f}>{FREQUENCY_LABELS[f]}</option>)}
                            </select>
                        </Row>

                        {value.frequency === 'weekly' && (
                            <Row label='On'>
                                <select
                                    name='schedule_weekday'
                                    className='form-select'
                                    value={value.weekday}
                                    onChange={event => set({weekday: Number(event.target.value)})}
                                >
                                    {WEEKDAYS.map((day, i) => <option key={day} value={i}>{day}</option>)}
                                </select>
                            </Row>
                        )}

                        {value.frequency === 'monthly' && (
                            <Row label='On day'>
                                <select
                                    name='schedule_day_of_month'
                                    className='form-select'
                                    value={value.dayOfMonth}
                                    onChange={event => set({dayOfMonth: Number(event.target.value)})}
                                >
                                    {Array.from({length: MAX_DAY_OF_MONTH}, (_, i) => i + 1).map(day => (
                                        <option key={day} value={day}>{day}</option>
                                    ))}
                                </select>
                                <div className='form-text'>Days 1 to 28, so that every month has the day.</div>
                            </Row>
                        )}

                        {value.frequency === 'hourly' ? (
                            <Row label='At minute'>
                                <input
                                    name='schedule_minute'
                                    type='number'
                                    min={0}
                                    max={59}
                                    className='form-control'
                                    value={value.minute}
                                    required={true}
                                    onChange={event => set({minute: event.target.value === '' ? '' : Number(event.target.value)})}
                                />
                            </Row>
                        ) : (
                            <Row label='At'>
                                <input
                                    name='schedule_time'
                                    type='time'
                                    className='form-control'
                                    value={value.time}
                                    required={true}
                                    onChange={event => set({time: event.target.value})}
                                />
                            </Row>
                        )}

                        <Row label=''>
                            <div className='form-text schedule-time-zone'>
                                Times in your time zone ({zone}); Motuz stores them in UTC, so after a change to or
                                from daylight saving time the copy runs an hour earlier or later.
                            </div>
                        </Row>

                        <Row label='Run once now'>
                            <Toggle
                                name='schedule_run_now'
                                className='form-label'
                                checked={value.runNow}
                                onChange={event => set({runNow: event.target.checked})}
                            />
                            <div className='form-text'>
                                {value.runNow ? 'The first copy starts now, then at the scheduled times.'
                                    : 'The first copy starts at the next scheduled time.'}
                            </div>
                        </Row>

                        <Row label='Existing files'>
                            <span className='form-label schedule-add-only'>
                                Only new files are copied; nothing in the destination is deleted or overwritten.
                                {this.props.generalOverwrites ? ' (This server allows overwriting files that changed.)' : ''}
                            </span>
                        </Row>

                        {egress && (
                            <div className='schedule-egress alert alert-warning mb-3'>
                                <div className='row mb-2'>
                                    <div className='col-5 text-end'>
                                        <label className='form-label' htmlFor='schedule-new-gb'>
                                            Expected new data per run (GB)
                                        </label>
                                    </div>
                                    <div className='col-4'>
                                        <input
                                            id='schedule-new-gb'
                                            name='schedule_new_gb'
                                            type='number'
                                            min={0}
                                            step='any'
                                            className='form-control form-control-sm'
                                            value={value.newGbTouched ? value.newGb : (defaultNewGb(estimate) ?? '')}
                                            onChange={event => set({newGb: event.target.value, newGbTouched: true, repeatConfirmed: false})}
                                        />
                                    </div>
                                </div>
                                <div className='schedule-egress-text mb-2'>{egress.text}</div>
                                <div className='form-check'>
                                    <input
                                        id='schedule-egress-confirm'
                                        name='schedule_egress_confirm'
                                        type='checkbox'
                                        className='form-check-input'
                                        required={true}
                                        checked={value.repeatConfirmed}
                                        onChange={event => set({repeatConfirmed: event.target.checked})}
                                    />
                                    <label className='form-check-label fw-bold' htmlFor='schedule-egress-confirm'>
                                        {egress.confirmLabel}
                                    </label>
                                </div>
                            </div>
                        )}
                    </>
                )}
            </details>
        );
    }
}

CopyJobSchedule.defaultProps = {
    value: initialSchedule(),
    estimate: null,
    generalOverwrites: false,
    onChange: () => {},
}

export default CopyJobSchedule;
