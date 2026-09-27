import React from 'react';

import {CUSTOM, describeSize, estimateMemory} from 'utils/copyPerformance.js';


// Help next to each field (what it does and when it helps)
const HELP = {
    transfers: 'Files copied at the same time. More helps with many files.',
    checkers: 'Files compared with the destination at the same time. More helps with many small files.',
    multi_thread_streams: 'Parallel streams per large file (0: off). More helps with few large files.',
    multi_thread_cutoff: 'Files above this size are copied with multi-thread streams.',
    s3_upload_concurrency: 'Parts of one file uploaded to S3 at the same time.',
    s3_chunk_size: 'Size of each uploaded S3 part; held in memory while it is sent.',
    azureblob_upload_concurrency: 'Blocks of one file uploaded to Azure at the same time.',
    azureblob_chunk_size: 'Size of each uploaded Azure block; held in memory while it is sent.',
};


/**
 * The collapsed "Performance" section of the New Copy Job dialog: a preset and the
 * fields of the destination's type (the server sends which, with their limits). Empty
 * fields mean the server's default, shown as placeholder. The dialog owns the values.
 */
export default class CopyJobPerformance extends React.PureComponent {
    render() {
        const {info, loadError, values, preset, errors, open, onToggle} = this.props;
        const presets = info ? info.presets : [];
        const current = presets.find(p => p.id === preset);
        const summary = preset === CUSTOM ? 'Custom' : (current ? current.label : 'Default');

        return (
            <details className='performance-section' open={open}
                     onToggle={event => onToggle(event.currentTarget.open)}>
                <summary className='text-primary h5 mt-4 mb-2'>
                    Performance <small className='text-muted fs-6 performance-summary'>{summary}</small>
                </summary>
                {loadError && (
                    <div className='text-muted mb-3'>
                        The performance settings could not be loaded; the job uses the server's defaults.
                    </div>
                )}
                {info && this.renderFields()}
            </details>
        );
    }

    renderFields() {
        const {info, values, preset, errors, onChange, onPreset} = this.props;
        const current = info.presets.find(p => p.id === preset);
        const memory = estimateMemory(values, info);
        const overBudget = memory !== null && memory > info.memory_budget;

        return (
            <React.Fragment>
                <div className="row mb-3">
                    <div className="col-4 text-end">
                        <label className='form-label fw-bold' htmlFor='performance-preset'>Preset</label>
                    </div>
                    <div className="col-7">
                        <select id='performance-preset' name='performance_preset' className='form-select'
                                value={preset} onChange={event => onPreset(event.target.value)}>
                            {info.presets.map(p => (
                                <option key={p.id} value={p.id} disabled={!p.available}>
                                    {p.label}{p.available ? '' : ' (not possible on this server)'}
                                </option>
                            ))}
                            <option value={CUSTOM}>Custom</option>
                        </select>
                        <div className='form-text performance-preset-description'>
                            {current ? current.description : 'Your own values'}
                            {current && current.reduced && ' (reduced to this server\'s limits)'}
                        </div>
                    </div>
                    <div className="col-1"></div>
                </div>

                {info.fields.map(field => (
                    <div className="row mb-2" key={field.name}>
                        <div className="col-4 text-end">
                            <label className='form-label fw-bold' htmlFor={`performance-${field.name}`}>
                                {field.label}
                            </label>
                        </div>
                        <div className="col-7">
                            <input
                                id={`performance-${field.name}`}
                                name={`performance_${field.name}`}
                                type='text'
                                inputMode={field.kind === 'int' ? 'numeric' : 'text'}
                                className={'form-control form-control-sm' + (errors[field.name] ? ' is-invalid' : '')}
                                placeholder={`default ${field.default}`}
                                value={values[field.name] || ''}
                                onChange={event => onChange(field.name, event.target.value)}
                            />
                            <div className={errors[field.name] ? 'invalid-feedback' : 'form-text'}>
                                {errors[field.name] || `${HELP[field.name] || ''} Max ${field.max}.`}
                            </div>
                        </div>
                        <div className="col-1"></div>
                    </div>
                ))}

                <div className="row mb-3">
                    <div className="col-4 text-end">
                        <span className='form-label fw-bold'>Memory</span>
                    </div>
                    <div className={'col-7 performance-memory' + (overBudget ? ' text-danger' : ' text-muted')}>
                        {memory === null ? 'Enter valid values to see the estimate'
                            : `About ${describeSize(memory)} of ${describeSize(info.memory_budget)} allowed per job`}
                    </div>
                    <div className="col-1"></div>
                </div>
            </React.Fragment>
        );
    }
}

CopyJobPerformance.defaultProps = {
    info: null,
    loadError: false,
    values: {},
    preset: 'default',
    errors: {},
    open: false,
    onToggle: (open) => {},
    onChange: (name, value) => {},
    onPreset: (id) => {},
}
