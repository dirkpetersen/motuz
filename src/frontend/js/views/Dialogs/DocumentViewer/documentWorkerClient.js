/**
 * Runs one operation of the document worker (workers/documentWorker.js) in a fresh
 * Web Worker, which is terminated when it answers or after `timeoutMs`: a hostile
 * file that makes the parser loop or eat memory never affects the page or the next
 * document. The file's bytes are transferred, not copied.
 */
export const WORKER_TIMEOUTS = Object.freeze({docx: 30000, pptx: 30000, sheet: 60000});

export class DocumentWorkerError extends Error {
    constructor(message, kind) {
        super(message);
        this.name = 'DocumentWorkerError';
        this.kind = kind || null;
    }
}

export function runDocumentWorker(op, buffer, container, {timeoutMs = WORKER_TIMEOUTS[op] || 30000, signal} = {}) {
    return new Promise((resolve, reject) => {
        let worker;
        try {
            worker = new Worker(new URL('../../../workers/documentWorker.js', import.meta.url));
        } catch (e) {
            reject(new DocumentWorkerError('This browser cannot open documents in a background worker'));
            return;
        }
        let done = false;
        const finish = (fn, value) => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            worker.terminate();
            if (signal) signal.removeEventListener('abort', onAbort);
            fn(value);
        };
        const onAbort = () => finish(reject, new DocumentWorkerError('Cancelled', 'AbortError'));
        const timer = setTimeout(() => finish(reject, new DocumentWorkerError(
            `Opening the document took longer than ${Math.round(timeoutMs / 1000)} seconds; `
            + 'it may be too large or complex to preview', 'Timeout')), timeoutMs);
        if (signal) {
            if (signal.aborted) {
                onAbort();
                return;
            }
            signal.addEventListener('abort', onAbort);
        }
        worker.onmessage = (event) => {
            const message = event.data || {};
            if (message.ok) {
                finish(resolve, message.result);
            } else {
                finish(reject, new DocumentWorkerError(message.error || 'The document could not be read', message.errorKind));
            }
        };
        worker.onerror = (event) => {
            event.preventDefault();
            finish(reject, new DocumentWorkerError('The document could not be read (the parser failed, '
                + 'possibly out of memory)', 'WorkerError'));
        };
        worker.postMessage({id: 1, op, data: buffer, container}, [buffer]);
    });
}
