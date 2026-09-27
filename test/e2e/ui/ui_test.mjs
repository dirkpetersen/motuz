// Playwright UI checks against the e2e stack (https://localhost), run by test/e2e/run.sh.
// PHASE=paste (suite ui, default): after the credentials suite (the picker checks use the
// files it leaves in alice's home), while the app uses rclone's OneDrive app (paste mode).
// PHASE=callback (suite ui-callback): after run.sh switched the app to an own OneDrive
// app, which sends the browser back to Motuz (/clouds?oauth_state=...).
// Chromium: playwright's own (npx playwright install chromium), or MOTUZ_E2E_CHROMIUM.
// Screenshots of failed flows go to $MOTUZ_E2E_LOGS (default test/e2e/logs), screenshots
// of the connection dialog states to $MOTUZ_E2E_SCREENSHOTS (if set).
import { chromium, request } from 'playwright';
import { mkdirSync } from 'fs';
import { execFileSync } from 'child_process';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const BASE = process.env.MOTUZ_E2E_BASE || 'https://localhost';
const PHASE = process.env.PHASE || 'paste';
const LOGS = process.env.MOTUZ_E2E_LOGS || join(dirname(fileURLToPath(import.meta.url)), '..', 'logs');
const SHOTS = process.env.MOTUZ_E2E_SCREENSHOTS || '';
mkdirSync(LOGS, { recursive: true });
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, ok, detail = '') {
    results.push(!!ok);
    console.log(`${ok ? 'PASS' : 'FAIL'} [${PHASE}] ${name}${ok ? '' : ' ' + JSON.stringify(detail)}`);
}

const browser = await chromium.launch({ executablePath: process.env.MOTUZ_E2E_CHROMIUM || undefined });
// The browser is in Los Angeles, the server (containers) in UTC: file times must be
// shown in the browser's zone
const context = await browser.newContext({
    ignoreHTTPSErrors: true, viewport: { width: 1400, height: 1300 },
    timezoneId: 'America/Los_Angeles', locale: 'en-US',
});
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', e => pageErrors.push(e.message));

// A flow that throws counts as one failed check, and the next flow still runs
async function flow(name, fn) {
    try {
        await fn();
    } catch (e) {
        check(`${name}: completed without error`, false, e.message.split('\n')[0]);
        await page.screenshot({ path: join(LOGS, `ui-${PHASE}-${name.replace(/\W+/g, '-')}.png`) }).catch(() => {});
        await page.keyboard.press('Escape').catch(() => {});
    }
}

async function shot(name, p = page) {
    if (SHOTS) {
        await p.waitForTimeout(200); // modal fade-in
        await p.locator('.modal-content').screenshot({ path: join(SHOTS, `${name}.png`) });
    }
}

async function pageShot(name) {
    if (SHOTS) {
        await page.screenshot({ path: join(SHOTS, `${name}.png`) });
    }
}

// Runs a shell command in the app container (fixtures), like common.py's sh()
function appShell(cmd, input) {
    const compose = (process.env.MOTUZ_E2E_COMPOSE || 'docker compose').split(' ');
    return execFileSync(compose[0], [...compose.slice(1), '-f', 'compose.yml', 'exec', '-T', 'app', 'sh', '-c', cmd],
        { cwd: join(dirname(fileURLToPath(import.meta.url)), '..'), input, encoding: 'utf8' });
}

// What a pane shows: its rows (name, age, age tooltip, size, selected, name tooltip)
// and the sort headers (aria-sort)
async function paneState(zone) {
    return page.evaluate(zone => {
        const root = document.querySelector(zone);
        const rows = [...root.querySelectorAll('.grid-file-name')].map(cell => {
            const age = cell.nextElementSibling;
            const size = age.nextElementSibling;
            return {
                name: cell.textContent.trim(), title: cell.getAttribute('title') || '',
                age: age.textContent.trim(), ageTitle: age.getAttribute('title') || '',
                size: size.textContent.trim(), active: cell.classList.contains('active'),
            };
        });
        const sort = Object.fromEntries([...root.querySelectorAll('.pane-header [role=columnheader]')]
            .map(h => [h.querySelector('button').dataset.sortColumn, h.getAttribute('aria-sort')]));
        return { rows, names: rows.map(r => r.name), selected: rows.filter(r => r.active).map(r => r.name), sort };
    }, zone);
}

// The viewer's pager: the lines in the DOM (numbers of "line NNNNNN ..."), its chunks, the
// line at the top and at the bottom of the viewport, the markers, the focus. With
// `scrollTo` ('end' or pixels) it first scrolls there and measures in the same task,
// i.e. before the viewer can react (load or add a chunk).
async function pagerState(scrollTo = null) {
    return page.evaluate(scrollTo => {
        const scroller = document.querySelector('.file-viewer-scroll');
        const pre = scroller && scroller.querySelector('.file-viewer-content');
        if (!pre) {
            return null;
        }
        if (scrollTo !== null) {
            scroller.scrollTop = scrollTo === 'end' ? scroller.scrollHeight : scrollTo;
        }
        const text = pre.textContent;
        const lines = text.split('\n');
        if (lines[lines.length - 1] === '') {
            lines.pop();
        }
        const numbers = lines.map(l => { const m = /^line (\d{6}) /.exec(l); return m ? Number(m[1]) : null; });
        const s = scroller.getBoundingClientRect();
        const p = pre.getBoundingClientRect();
        const lineAt = y => {
            const r = document.caretRangeFromPoint(s.left + 20, y);
            if (!r || !pre.contains(r.startContainer)) {
                return null;
            }
            const t = r.startContainer.textContent;
            const start = t.lastIndexOf('\n', r.startOffset - 1) + 1;
            const m = /^line (\d{6}) /.exec(t.slice(start, start + 12));
            return m ? Number(m[1]) : null;
        };
        return {
            lines: lines.length, first: numbers[0], last: numbers[numbers.length - 1],
            sequential: numbers.every((n, i) => n !== null && (i === 0 || n === numbers[i - 1] + 1)),
            chunks: pre.children.length, textLength: text.length,
            top: lineAt(Math.max(s.top, p.top) + 5), bottom: lineAt(Math.min(s.bottom, p.bottom) - 5),
            scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight,
            atBottom: scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 2,
            focused: document.activeElement === scroller,
            status: (document.querySelector('.file-viewer-status') || {}).textContent || '',
            eof: !!document.querySelector('.file-viewer-eof'),
        };
    }, scrollTo);
}

async function chunkSignature() {
    return page.evaluate(() => [...document.querySelectorAll('.file-viewer-content span')].map(s => s.dataset.chunk).join());
}

async function waitPagerIdle() {
    await page.waitForFunction(() => document.querySelector('.file-viewer-scroll')?.getAttribute('aria-busy') === 'false',
        null, { timeout: 20000 });
}

async function waitChunksChanged(before) {
    await page.waitForFunction(before => [...document.querySelectorAll('.file-viewer-content span')]
        .map(s => s.dataset.chunk).join() !== before, before, { timeout: 20000 });
    await waitPagerIdle();
}

async function openClouds() {
    await page.click('text=My Cloud Connections');
    await page.waitForSelector('button:has-text("New Connection")');
}

async function newConnection(type) {
    await page.click('button:has-text("New Connection")');
    await page.waitForSelector('select[name=type]');
    await page.selectOption('select[name=type]', type);
}

async function closeDialog(p = page) {
    await p.click('.modal-footer button:has-text("Cancel")');
    await p.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
}

// What the user sees in the connection dialog: visible field labels, text fields, the
// footer's buttons and hint, enabled primary buttons, and what the sign-in part contains
async function dialog(p = page) {
    return p.evaluate(() => {
        const modal = document.querySelector('.modal-content');
        const visible = el => el.checkVisibility(); // false inside a closed <details> too
        const text = el => el.textContent.trim().replace(/\s+/g, ' ');
        const all = sel => [...modal.querySelectorAll(sel)].filter(visible);
        const signIn = modal.querySelector('.oauth-sign-in');
        return {
            labels: all('.form-label').map(text),
            nameInputs: all('input[name=name]').length,
            textInputs: all('input:not([type=hidden]), textarea').map(i => i.name || i.getAttribute('aria-label') || ''),
            footer: all('.modal-footer button').map(b => ({ text: text(b), disabled: b.disabled, primary: b.classList.contains('btn-primary') })),
            hint: all('.modal-footer .footer-hint').map(text).join(' '),
            error: all('.dialog-error').map(text).join(' '),
            enabledPrimary: all('.btn-primary').filter(b => !b.disabled).map(b => ({ text: text(b), inFooter: !!b.closest('.modal-footer') })),
            signInVisible: !!signIn && visible(signIn),
            signInControls: signIn ? [...signIn.querySelectorAll('input:not([type=hidden]), textarea, button, select')]
                .filter(visible).map(c => (c.tagName + ':' + (c.getAttribute('aria-label') || text(c))).slice(0, 60)) : [],
            account: all('.oauth-account').map(text).join(' '),
            type: modal.querySelector('select[name=type]').value,
            name: (modal.querySelector('input[name=name]') || {}).value,
        };
    });
}

const footerTexts = d => d.footer.map(b => b.text);
const nameLabels = d => d.labels.filter(l => /name/i.test(l));

// Exactly one Connection Name field and exactly one enabled primary button, the footer's
// "Create connection"
function checkOneNameOneCreate(what, d) {
    check(`${what}: exactly one name field, "Connection Name"`, d.nameInputs === 1 && nameLabels(d).length === 1
        && nameLabels(d)[0] === 'Connection Name' && d.textInputs.filter(n => /name/i.test(n)).length === 1, d);
    check(`${what}: exactly one enabled primary button, "Create connection" in the footer`, d.enabledPrimary.length === 1
        && d.enabledPrimary[0].text === 'Create connection' && d.enabledPrimary[0].inFooter, d.enabledPrimary);
}

// The sign-in has no name field and no create button of its own
function checkSignInPanel(what, d) {
    check(`${what}: no create or name controls in the sign-in part`, d.signInVisible
        && !d.signInControls.some(c => /create|name/i.test(c)), d.signInControls);
}

// "Sign in with ..." in paste mode through fake_ms.py: returns the dialog state after
// pasting the address rclone's app would have been sent to
async function signInPaste(buttonText, loopback, shotName) {
    let redirected = '';
    const listener = r => { if (r.url().startsWith(loopback)) redirected = r.url(); };
    context.on('request', listener);
    const [popup] = await Promise.all([context.waitForEvent('page'), page.click(`button:has-text("${buttonText}")`)]);
    for (let i = 0; i < 50 && !redirected; i++) await page.waitForTimeout(200);
    context.off('request', listener);
    check(`${buttonText}: popup ends at the rclone redirect with code and state`, redirected.includes('code=') && redirected.includes('state='), redirected);
    await popup.close();
    let d = await dialog();
    check(`${buttonText}: while pasting, the footer still has only Cancel and a disabled Create connection`,
        JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]' && d.footer[1].disabled, d.footer);
    await page.fill('.oauth-sign-in textarea', redirected);
    await shot(shotName);
    await page.click('.oauth-sign-in button:has-text("Continue")');
    await page.waitForSelector('.oauth-account', { timeout: 20000 });
    return dialog();
}

// Before signing in: the sign-in button is the only prominent action
function checkBeforeSignIn(what, d, buttonText) {
    check(`${what} before sign-in: the only enabled primary button is "${buttonText}"`, d.enabledPrimary.length === 1
        && d.enabledPrimary[0].text === buttonText && !d.enabledPrimary[0].inFooter, d.enabledPrimary);
    check(`${what} before sign-in: footer is Cancel and a disabled Create connection, hint "Sign in first"`,
        JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]' && d.footer[1].disabled && d.footer[1].primary
        && d.hint === 'Sign in first', d);
    check(`${what} before sign-in: one name field, "Connection Name"`, d.nameInputs === 1 && nameLabels(d).length === 1, d.labels);
    checkSignInPanel(`${what} before sign-in`, d);
}

async function waitForListed(name) {
    await page.waitForSelector('.modal-content', { state: 'detached', timeout: 15000 });
    await page.waitForSelector(`table >> text="${name}"`, { timeout: 15000 });
}

// Fixtures through the API: two folders in alice's home for the drag and drop copy
const api = await request.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
const login = await (await api.post('/api/auth/login/', { data: { username: 'alice', password: 'AlicePass1' } })).json();
const auth = { Authorization: 'Bearer ' + login.access };
async function connection(name) {
    const all = await (await api.get('/api/connections/', { headers: auth })).json();
    return all.find(c => c.name === name);
}

async function loginUi() {
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('input[name=username]');
    await page.fill('input[name=username]', 'alice');
    await page.fill('input[name=password]', 'AlicePass1');
    await page.keyboard.press('Enter');
    await page.waitForSelector('.grid-files', { timeout: 20000 });
}

if (PHASE === 'paste') {
    for (const path of ['/home/alice/ui-src/sub', '/home/alice/ui-dst', '/home/alice/ui-perf-dst']) {
        await api.post('/api/system/files/mkdir/', { headers: auth, data: { path, connection_id: 0 } });
    }

    await flow('login', async () => {
        await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('input[name=username]');
        check('login page describes Motuz', (await page.textContent('.login-about')).includes('large data transfers'));
        check('login page links to privacy policy and terms',
            await page.locator('.login-footer a[href="/privacy"]').count() === 1
            && await page.locator('.login-footer a[href="/terms"]').count() === 1);
        await page.fill('input[name=username]', 'alice');
        await page.fill('input[name=password]', 'wrong');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(1500);
        check('wrong password: still on the login form', await page.isVisible('input[name=password]'));
        await page.fill('input[name=password]', 'AlicePass1');
        await page.keyboard.press('Enter');
        await page.waitForSelector('.grid-files', { timeout: 20000 });
        await page.waitForSelector('.grid-files >> text=ui-src', { timeout: 20000 });
        const body = await page.textContent('body');
        check('logged in: file browser shows /home/alice', body.includes('/home/alice'));
        check('both panes list the home directory', await page.locator('.grid-files').count() === 2
            && await page.locator('.grid-files').nth(1).getByText('ui-dst', { exact: true }).count() === 1);
        check('app footer links to privacy policy and terms',
            await page.locator('#zone-status-bar a[href="/privacy"]').isVisible()
            && await page.locator('#zone-status-bar a[href="/terms"]').isVisible());
    });

    await flow('drag-and-drop copy', async () => {
        let posted = null;
        const listener = r => { if (r.url().endsWith('/api/copy-jobs/') && r.method() === 'POST') posted = r.postDataJSON(); };
        page.on('request', listener);
        const panes = page.locator('.grid-files');
        await panes.nth(0).getByText('ui-src', { exact: true }).dragTo(panes.nth(1).getByText('ui-dst', { exact: true }));
        await page.waitForSelector('.modal-content', { timeout: 10000 });
        check('drop opens the copy dialog', true);
        await page.click('.modal-content button[type=submit]');
        for (let i = 0; i < 50 && !posted; i++) await page.waitForTimeout(200);
        page.off('request', listener);
        check('copy job posted with the dragged source and target', posted && posted.src_resource_path === '/home/alice/ui-src'
            && posted.dst_resource_path.startsWith('/home/alice/ui-dst'), posted);
        let job = null;
        for (let i = 0; i < 60; i++) {
            const jobs = await (await api.get('/api/copy-jobs/', { headers: auth })).json();
            job = jobs.data.find(j => j.src_resource_path === '/home/alice/ui-src');
            if (job && job.progress_state !== 'PROGRESS' && job.progress_state !== 'PENDING') break;
            await page.waitForTimeout(1000);
        }
        check('copy job SUCCESS', job && job.progress_state === 'SUCCESS', job && job.progress_state);
        await page.waitForFunction(() => document.querySelector('table') && document.querySelector('table').innerText.includes('ui-src'),
                                   null, { timeout: 15000 });
        check('job table shows the job', true);
        // Submitting opens the job's progress dialog
        await page.click('.modal-content button:has-text("Close")');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
    });

    // The collapsed Performance section of the New Copy Job dialog (utils/copyPerformance.js,
    // GET /api/copy-jobs/performance/). compose.yml: MOTUZ_RCLONE_CHECKERS=16, MAX_TRANSFERS=48
    await flow('copy performance', async () => {
        const posts = [];
        const listener = r => { if (r.url().endsWith('/api/copy-jobs/') && r.method() === 'POST') posts.push(r.postDataJSON()); };
        page.on('request', listener);
        const panes = page.locator('.grid-files');
        const openDialog = async () => {
            await panes.nth(0).getByText('ui-src', { exact: true }).dragTo(panes.nth(1).getByText('ui-perf-dst', { exact: true }));
            await page.waitForSelector('.modal-content .performance-section #performance-preset', { state: 'attached', timeout: 10000 });
        };
        await openDialog();
        check('performance: section collapsed, preset Default',
            !(await page.locator('.performance-section').evaluate(d => d.open))
            && (await page.textContent('.performance-summary')).trim() === 'Default');
        await page.click('.performance-section summary');
        const labels = await page.$$eval('.performance-section label', ls => ls.map(l => l.textContent.trim()));
        check('performance: local destination has no S3/Azure fields', JSON.stringify(labels) === JSON.stringify(
            ['Preset', 'Parallel transfers', 'Parallel checkers', 'Multi-thread streams', 'Multi-thread cutoff']), labels);
        check('performance: server defaults as placeholders',
            await page.getAttribute('#performance-checkers', 'placeholder') === 'default 16'
            && await page.getAttribute('#performance-transfers', 'placeholder') === 'default 4');
        await page.selectOption('#performance-preset', 'small_files');
        check('performance: "Many small files" fills the fields',
            await page.inputValue('#performance-transfers') === '32' && await page.inputValue('#performance-checkers') === '64'
            && await page.inputValue('#performance-multi_thread_streams') === '');
        await shot('copy-performance-small-files');

        // Invalid values are shown and not posted
        await page.fill('#performance-transfers', '4 --config=/etc/shadow');
        check('performance: editing a field switches to Custom', await page.inputValue('#performance-preset') === 'custom');
        await page.click('.modal-footer button[type=submit]');
        await page.waitForSelector('.dialog-error', { timeout: 5000 });
        check('performance: invalid value shown in the dialog, nothing posted',
            (await page.textContent('.dialog-error')).includes('must be a whole number') && posts.length === 0
            && await page.locator('#performance-transfers.is-invalid').count() === 1);
        await page.fill('#performance-transfers', '49');
        check('performance: above the cap is invalid',
            (await page.textContent('.performance-section .invalid-feedback')).includes('between 1 and 48'));
        await shot('copy-performance-invalid');

        await page.selectOption('#performance-preset', 'small_files');
        await page.click('.modal-footer button[type=submit]');
        for (let i = 0; i < 50 && !posts.length; i++) await page.waitForTimeout(200);
        check('performance: POST carries the preset', posts.length === 1
            && JSON.stringify(posts[0].performance) === JSON.stringify({ transfers: 32, checkers: 64 }), posts);
        await page.waitForSelector('.modal-content .job-performance', { timeout: 10000 });
        check('performance: job detail shows the settings',
            (await page.textContent('.modal-content .job-performance')).trim() === '32 transfers · 64 checkers');
        let job = null;
        for (let i = 0; i < 60; i++) {
            const jobs = await (await api.get('/api/copy-jobs/', { headers: auth })).json();
            job = jobs.data.find(j => (j.dst_resource_path || '').startsWith('/home/alice/ui-perf-dst'));
            if (job && job.progress_state !== 'PROGRESS') break;
            await page.waitForTimeout(1000);
        }
        check('performance: job SUCCESS with its settings', job && job.progress_state === 'SUCCESS'
            && JSON.stringify(job.performance) === JSON.stringify({ transfers: 32, checkers: 64 }), job);
        page.off('request', listener);
        await page.click('.modal-footer button:has-text("Close")');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
    });

    await flow('onedrive-sign-in', async () => {
        await openClouds();
        await newConnection('onedrive');
        let d = await dialog();
        checkBeforeSignIn('OneDrive', d, 'Sign in with Microsoft');
        await shot('onedrive-before-sign-in');

        d = await signInPaste('Sign in with Microsoft', 'http://localhost:53682/', 'onedrive-pasting-address');
        check('OneDrive: "Signed in as" the account', d.account.includes('Signed in as Alice Test'), d.account);
        const options = await page.$$eval('.oauth-sign-in select option', os => os.map(o => o.textContent));
        check('OneDrive: drives offered', options.some(o => o.includes('OneDrive (Alice Test)')) && options.some(o => o.includes('Lab Share - Documents')), options);
        check('OneDrive: Connection Name prefilled with the drive', d.name === 'OneDrive (Alice Test)', d.name);
        await page.selectOption('.oauth-sign-in select', 'b!labdocs');
        check('OneDrive: Connection Name follows the drive', await page.inputValue('input[name=name]') === 'Lab Share - Documents');
        await page.fill('input[name=name]', 'ui-onedrive');
        await page.selectOption('.oauth-sign-in select', 'b!mydrive');
        check('OneDrive: a typed Connection Name stays when the drive changes', await page.inputValue('input[name=name]') === 'ui-onedrive');
        d = await dialog();
        checkOneNameOneCreate('OneDrive after sign-in', d);
        checkSignInPanel('OneDrive after sign-in', d);
        check('OneDrive after sign-in: footer is Cancel and Create connection, no test button, no hint',
            JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]' && !d.hint && !d.error, d);
        await shot('onedrive-after-sign-in');
        await page.click('.modal-footer button:has-text("Create connection")');
        await waitForListed('ui-onedrive');
        const conn = await connection('ui-onedrive');
        check('OneDrive: connection created with the chosen drive', conn && conn.type === 'onedrive'
            && conn.onedrive_drive_id === 'b!mydrive' && conn.onedrive_drive_type === 'business', conn);
    });

    await flow('onedrive-paste-token', async () => {
        await newConnection('onedrive');
        await page.click('summary:has-text("Advanced: paste a token")');
        await page.waitForSelector('input[name=onedrive_token]'); // the toggle event is asynchronous
        let d = await dialog();
        check('Advanced paste token: the sign-in is hidden', !d.signInVisible, d);
        check('Advanced paste token: footer is Test connection, Cancel, Create connection',
            JSON.stringify(footerTexts(d)) === '["Test connection","Cancel","Create connection"]', d.footer);
        checkOneNameOneCreate('Advanced paste token', d);
        check('Advanced paste token: drive and token fields shown', d.textInputs.includes('onedrive_drive_id') && d.textInputs.includes('onedrive_token'), d.textInputs);
        await shot('advanced-paste-token');
        await page.click('summary:has-text("Advanced: paste a token")');
        await page.waitForSelector('.modal-footer .footer-hint');
        d = await dialog();
        check('Advanced closed: back to the sign-in footer', d.signInVisible && d.hint === 'Sign in first'
            && JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]', d);
        await closeDialog();
    });

    await flow('gdrive-sign-in', async () => {
        await newConnection('drive');
        let d = await dialog();
        checkBeforeSignIn('Google Drive', d, 'Sign in with Google');
        await shot('gdrive-before-sign-in');
        d = await signInPaste('Sign in with Google', 'http://127.0.0.1:53682/', 'gdrive-pasting-address');
        check('Google Drive: "Signed in as" the account', d.account.includes('Signed in as alice@example.org'), d.account);
        const options = await page.$$eval('.oauth-sign-in select option', os => os.map(o => o.textContent));
        check('Google Drive: My Drive and the shared drive offered', JSON.stringify(options) === '["My Drive (alice@example.org)","Shared drive: Lab Team"]', options);
        check('Google Drive: Connection Name prefilled with the drive', d.name === 'My Drive (alice@example.org)', d.name);
        await page.selectOption('.oauth-sign-in select', '0ALABTEAM');
        d = await dialog();
        check('Google Drive: Connection Name follows the drive', d.name === 'Shared drive: Lab Team', d.name);
        checkOneNameOneCreate('Google Drive after sign-in', d);
        checkSignInPanel('Google Drive after sign-in', d);
        check('Google Drive after sign-in: footer is Cancel and Create connection',
            JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]' && !d.hint && !d.error, d);
        await shot('gdrive-after-sign-in');
        await page.click('.modal-footer button:has-text("Create connection")');
        await waitForListed('Shared drive: Lab Team');
        const conn = await connection('Shared drive: Lab Team');
        check('Google Drive: connection created for the shared drive', conn && conn.type === 'drive'
            && conn.gdrive_team_drive === '0ALABTEAM', conn);
    });

    await flow('edit-oauth-connection', async () => {
        await page.click('table >> text="ui-onedrive"');
        await page.waitForSelector('.modal-content');
        const d = await dialog();
        check('Edit OneDrive: one name field, no sign-in', d.nameInputs === 1 && nameLabels(d).length === 1 && !d.signInVisible, d);
        check('Edit OneDrive: footer is Delete, Test connection, Cancel, Save changes; one enabled primary',
            JSON.stringify(footerTexts(d)) === '["Delete","Test connection","Cancel","Save changes"]'
            && d.enabledPrimary.length === 1 && d.enabledPrimary[0].text === 'Save changes', d.footer);
        check('Edit OneDrive: drive and token collapsed', !d.textInputs.includes('onedrive_token'), d.textInputs);
        await shot('edit-onedrive');
        // Test connection must test the stored (brokered) token: the request carries the id.
        // rclone then fails at the real Graph API with the fake token, but never with "empty token".
        const before = await connection('ui-onedrive');
        const [verifyRequest, verifyResponse] = await Promise.all([
            page.waitForRequest(r => r.url().endsWith('/api/connections/verify/')),
            page.waitForResponse(r => r.url().endsWith('/api/connections/verify/'), { timeout: 60000 }),
            page.click('.modal-footer button:has-text("Test connection")'),
        ]);
        const verifyBody = verifyRequest.postDataJSON() || {};
        const verifyResult = await verifyResponse.json().catch(() => ({}));
        check('Edit OneDrive: Test connection sends the connection id', before && verifyBody.id === before.id, verifyBody);
        check('Edit OneDrive: Test connection uses the stored token', !/empty token/i.test(verifyResult.message || ''), verifyResult);
        await page.fill('input[name=name]', 'ui-onedrive-renamed');
        await page.click('.modal-footer button:has-text("Save changes")');
        await waitForListed('ui-onedrive-renamed');
        const conn = await connection('ui-onedrive-renamed');
        check('Edit OneDrive: renamed, drive kept', conn && conn.onedrive_drive_id === 'b!mydrive', conn);
    });

    await flow('oauth-error-callback', async () => {
        // The backend sends the browser here when an automatic sign-in expired
        await page.goto(BASE + '/clouds?oauth_error=expired&oauth_provider=gdrive', { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('.modal-content .dialog-error', { timeout: 15000 });
        const d = await dialog();
        check('failed automatic sign-in: dialog on Google Drive with one plain error above the buttons', d.type === 'drive'
            && d.error.includes('Please sign in again') && d.hint === 'Sign in first', d);
        await shot('sign-in-error');
        await closeDialog();
    });

    await flow('credentials-picker', async () => {
        const responses = [];
        const listener = async r => { if (r.url().includes('/local-credentials/')) responses.push(await r.text().catch(() => '')); };
        page.on('response', listener);
        await newConnection('s3');
        const picker = page.locator('.local-credentials');
        await picker.waitFor({ timeout: 15000 });
        check('S3: "Credentials" offers what was found in the home directory', (await picker.locator('.form-label').innerText()) === 'Credentials'
            && await picker.locator('optgroup[label="Found in your home directory"]').count() === 1);
        const options = await picker.locator('select option').allInnerTexts();
        const disabled = await picker.locator('select option[disabled]').allInnerTexts();
        check('S3: profiles and rclone remotes offered', options.some(o => o.startsWith('AWS profile: e2e-static'))
            && options.some(o => o.startsWith('rclone remote: s3remote')), options);
        check('S3: MFA role and credential_process profiles disabled', disabled.some(o => o.includes('role-mfa')) && disabled.some(o => o.includes('proc')), disabled);

        // Keys entered by hand
        let d = await dialog();
        check('S3 manual: key fields shown', d.textInputs.includes('s3_access_key_id') && d.textInputs.includes('s3_secret_access_key'), d.textInputs);
        check('S3 manual: footer is Test connection, Cancel, Create connection',
            JSON.stringify(footerTexts(d)) === '["Test connection","Cancel","Create connection"]', d.footer);
        checkOneNameOneCreate('S3 manual', d);
        check('S3 manual: "Bucket" and "Connection Name" are different fields', d.labels.includes('Bucket') && d.textInputs.includes('bucket'), d.labels);
        check('S3 manual: KMS key and endpoint collapsed under Advanced options', !d.textInputs.includes('kms_encryption_key_arn')
            && !d.textInputs.includes('s3_endpoint'), d.textInputs);
        await page.fill('input[name=name]', 'ui-s3-keys');
        await page.fill('input[name=s3_access_key_id]', 'AKIAIOSFODNN7EXAMPLE');
        await page.fill('input[name=s3_secret_access_key]', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY');
        await shot('s3-manual');
        await page.click('.modal-footer button:has-text("Create connection")');
        await waitForListed('ui-s3-keys');
        const keys = await connection('ui-s3-keys');
        check('S3 manual: connection created with the keys', keys && keys.subtype === 'key' && keys.s3_access_key_id === 'AKIAIOSFODNN7EXAMPLE', keys);

        // A profile from the home directory
        await newConnection('s3');
        await picker.waitFor({ timeout: 15000 });
        const value = await picker.locator('select option', { hasText: 'AWS profile: e2e-static' }).getAttribute('value');
        await picker.locator('select').selectOption(value);
        await page.waitForTimeout(300);
        check('S3 profile: selecting a profile hides the key fields and fills the region',
            await page.locator('input[name=s3_access_key_id]').count() === 0
            && await page.locator('select[name=subtype]').count() === 0
            && await page.locator('input[name=s3_region]').inputValue() === 'us-west-2'
            && await page.locator('input[type=hidden][name=profile_name]').getAttribute('value') === 'e2e-static');
        d = await dialog();
        check('S3 profile: footer is Test connection, Cancel, Create connection',
            JSON.stringify(footerTexts(d)) === '["Test connection","Cancel","Create connection"]', d.footer);
        checkOneNameOneCreate('S3 profile', d);
        await page.fill('input[name=name]', 'ui-s3-profile');
        await shot('s3-profile');
        await page.click('.modal-footer button:has-text("Create connection")');
        await waitForListed('ui-s3-profile');
        const prof = await connection('ui-s3-profile');
        check('S3 profile: connection created from the profile, without keys', prof && prof.subtype === 'profile'
            && prof.profile_name === 'e2e-static' && !prof.s3_access_key_id, prof);

        await newConnection('azureblob');
        await page.locator('.local-credentials select').waitFor({ timeout: 15000 });
        const azure = page.locator('.local-credentials select option', { hasText: 'rclone remote: azurite' });
        check('Azure: the Azurite rclone remote offered', await azure.count() === 1);
        await page.locator('.local-credentials select').selectOption(await azure.getAttribute('value'));
        await page.fill('input[name=name]', 'ui-azurite');
        await page.fill('input[name=bucket]', 'motuztest');
        await page.click('.modal-footer button:has-text("Test connection")');
        await page.waitForSelector('.verify-status:has-text("Connection works"), .verify-status:has-text("Test failed")', { timeout: 60000 });
        check('Azure: Test connection works', await page.locator('.verify-status:has-text("Connection works")').count() === 1,
            await page.locator('.modal-footer').innerText());
        await page.click('.modal-footer button:has-text("Create connection")');
        await waitForListed('ui-azurite');
        check('Azure: connection created and listed', true);

        await newConnection('webdav');
        await page.waitForTimeout(500);
        check('WebDAV: no credentials picker', await page.locator('.local-credentials').count() === 0);
        await page.fill('input[name=name]', 'ui-webdav');
        await page.fill('input[name=webdav_url]', 'http://127.0.0.1:9/');
        await page.fill('input[name=webdav_user]', 'alice');
        await page.fill('input[name=webdav_pass]', 'nope');
        await page.click('.modal-footer button:has-text("Test connection")');
        await page.waitForSelector('.verify-status:has-text("Test failed")', { timeout: 60000 });
        d = await dialog();
        check('WebDAV: a failed test is explained once, above the buttons', d.error.startsWith('The test failed')
            && await page.locator('.modal-content .dialog-error').count() === 1, d.error);
        await closeDialog();
        page.off('response', listener);
        // Metadata only: masked key ids, no Azure account key (Azurite's well-known dev key)
        const profiles = responses.flatMap(r => { try { return JSON.parse(r).profiles || []; } catch (e) { return []; } });
        check('local-credentials responses: masked key ids, no account key', profiles.length > 0
            && profiles.every(p => !p.access_key_id || p.access_key_id.startsWith('****'))
            && responses.every(r => !r.includes('Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6')), profiles.map(p => p.access_key_id));
    });

    // ---------------------------------------------------------------- file browser
    // /home/alice/ui-sort: names, ages and sizes that sort differently by each column
    //   name A-Z:  dira dirB | File1.txt file2.txt file10.txt image.bin
    //   age:       dirB (1 day) dira (10 days) | file10.txt (now) file2.txt (2 h) File1.txt (3 days) image.bin (2023)
    //   size:      File1.txt 10, file2.txt 100, image.bin 300, file10.txt 5000
    const FILE2 = 'hello from file2\n<b>not bold</b>\n' + 'z'.repeat(66) + '\n';
    const NUMBERED_LINES = 105000; // dira/numbered.log, 5,144,930 bytes
    const FOLLOW_LINES = 300; // dirB/follow.log, "line NNNNNN of the followed log"
    const FOLLOW_LOG = '/home/alice/ui-sort/dirB/follow.log';
    appShell('sudo -u alice python3 -', `
import os, time
d = '/home/alice/ui-sort'
for sub in ('dira', 'dirB'):
    os.makedirs(os.path.join(d, sub), exist_ok=True)
now = time.time()
def put(name, data, mtime):
    path = os.path.join(d, name)
    with open(path, 'wb') as f:
        f.write(data)
    os.utime(path, (mtime, mtime))
put('File1.txt', b'0123456789', now - 3 * 86400)
put('file2.txt', ${JSON.stringify(FILE2)}.encode(), now - 7200)
put('file10.txt', b'y' * 4999 + b'\\n', now - 5)
# ~5 MiB of numbered lines for the pager, as test/e2e/common.py's numbered_log()
put('dira/numbered.log', b''.join(('line %06d %s of a numbered log\\n' % (i, '\\u00fc' * (i % 5) + 'x' * (i % 29))).encode()
                                  for i in range(1, ${NUMBERED_LINES} + 1)), now - 60)
put('image.bin', b'\\x89PNG\\r\\n\\x1a\\n\\x00\\x00\\x00\\rIHDR' + bytes(range(256)) + b'\\x00' * 19, 1700000000)
# a log for the follow mode (tail -f), appended to while the viewer follows it
put('dirB/follow.log', b''.join(b'line %06d of the followed log\\n' % i for i in range(1, ${FOLLOW_LINES} + 1)), now - 60)
os.utime(os.path.join(d, 'dira'), (now - 10 * 86400,) * 2)
os.utime(os.path.join(d, 'dirB'), (now - 86400,) * 2)
`);
    const LEFT = '#zone-left-pane';
    const leftPane = page.locator(`${LEFT} .grid-files`);
    const sortBy = async column => {
        await page.click(`${LEFT} .pane-sort[data-sort-column=${column}]`);
        await page.waitForTimeout(100);
    };
    const row = name => leftPane.locator('.grid-file-name', { hasText: new RegExp(`^${name.replace('.', '\\.')}$`) });
    async function openUiSort() {
        await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
        await leftPane.getByText('ui-sort', { exact: true }).waitFor({ timeout: 20000 });
        await leftPane.getByText('ui-sort', { exact: true }).dblclick();
        await leftPane.getByText('file10.txt', { exact: true }).waitFor({ timeout: 20000 });
    }
    async function copyDialogResources() {
        await page.waitForSelector('.modal-content', { timeout: 10000 });
        const resources = await page.$$eval('.modal-content .row', rows => rows
            .filter(r => (r.querySelector('b') || {}).textContent === 'Resource')
            .map(r => r.querySelector('.col-7 span').textContent.trim()));
        await page.click('.modal-footer button:has-text("Close")');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
        return resources;
    }

    await flow('age-column', async () => {
        await openUiSort();
        const s = await paneState(LEFT);
        const by = Object.fromEntries(s.rows.map(r => [r.name, r]));
        check('age: sensible relative ages', /^(just now|\d+ sec ago)$/.test(by['file10.txt'].age) && by['file2.txt'].age === '2 h ago'
            && by['File1.txt'].age === '3 days ago' && /^\d+ years? ago$/.test(by['image.bin'].age)
            && by['dirB'].age === '1 day ago' && by['dira'].age === '10 days ago' && by['..'].age === '', s.rows);
        check('age: tooltip is the exact time in the browser\'s zone (server in UTC, browser in Los Angeles)',
            /^Nov 14, 2023, 2:13:20\sPM PST$/.test(by['image.bin'].ageTitle) && /PDT$/.test(by['file2.txt'].ageTitle), [by['image.bin'].ageTitle, by['file2.txt'].ageTitle]);
        check('size column: bytes and "Folder"', by['file10.txt'].size === '5 KiB' && by['File1.txt'].size === '10 B' && by['dira'].size === 'Folder', s.rows);
        check('rows explain double-click', by['file2.txt'].title.includes('Double-click to view') && by['dira'].title.includes('Double-click to open'), [by['file2.txt'].title, by['dira'].title]);
        const tz = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
        check('browser runs in America/Los_Angeles', tz === 'America/Los_Angeles', tz);
        // The name truncates with an ellipsis before the age and size columns shrink
        const layout = await page.evaluate(zone => {
            const cells = [...document.querySelector(zone).querySelectorAll('.grid-files > div')].slice(0, 3);
            const header = [...document.querySelector(zone).querySelectorAll('.pane-header [role=columnheader]')];
            return { widths: cells.map(c => Math.round(c.getBoundingClientRect().width)),
                     lefts: cells.map(c => Math.round(c.getBoundingClientRect().left)),
                     headerLefts: header.map(c => Math.round(c.getBoundingClientRect().left)),
                     ellipsis: getComputedStyle(cells[0]).textOverflow };
        }, LEFT);
        check('age and size columns keep their width, aligned with their headers', layout.widths[1] === 120 && layout.widths[2] === 96
            && layout.ellipsis === 'ellipsis' && JSON.stringify(layout.lefts) === JSON.stringify(layout.headerLefts), layout);
        await pageShot('pane-age-sort-by-name');
    });

    await flow('sorting', async () => {
        let s = await paneState(LEFT);
        check('default sort: name A-Z, natural and case-insensitive, .. and folders first',
            JSON.stringify(s.names) === '["..","dira","dirB","File1.txt","file2.txt","file10.txt","image.bin"]'
            && s.sort.name === 'ascending' && s.sort.age === 'none', s);
        await sortBy('age');
        s = await paneState(LEFT);
        check('sort by age: newest first', JSON.stringify(s.names) === '["..","dirB","dira","file10.txt","file2.txt","File1.txt","image.bin"]'
            && s.sort.age === 'ascending' && s.sort.name === 'none', s);
        await sortBy('age');
        s = await paneState(LEFT);
        check('sort by age again: oldest first, .. still on top', JSON.stringify(s.names) === '["..","dira","dirB","image.bin","File1.txt","file2.txt","file10.txt"]'
            && s.sort.age === 'descending', s);
        await sortBy('size');
        s = await paneState(LEFT);
        check('sort by size: smallest first, folders first', JSON.stringify(s.names) === '["..","dira","dirB","File1.txt","file2.txt","image.bin","file10.txt"]'
            && s.sort.size === 'ascending', s);
        await sortBy('size');
        s = await paneState(LEFT);
        check('sort by size again: largest first', JSON.stringify(s.names) === '["..","dira","dirB","file10.txt","image.bin","file2.txt","File1.txt"]'
            && s.sort.size === 'descending', s);
        check('sort arrow shown on the sorted column only', await page.locator(`${LEFT} .pane-sort-arrow`).count() === 1
            && await page.locator(`${LEFT} .pane-sort.active[data-sort-column=size] .pane-sort-arrow`).count() === 1);
        await pageShot('pane-sorted-by-size');

        // Select two files in this order, re-sort: the same two stay selected and are copied
        await row('file2.txt').click();
        await row('file10.txt').click({ modifiers: ['Control'] });
        s = await paneState(LEFT);
        check('two files selected', JSON.stringify(s.selected) === '["file10.txt","file2.txt"]', s.selected);
        await sortBy('name');
        s = await paneState(LEFT);
        check('after re-sorting, the same two files stay selected', JSON.stringify(s.selected) === '["file2.txt","file10.txt"]'
            && s.sort.name === 'ascending', s);
        await page.click('#zone-left-commands button.btn-lg');
        let resources = await copyDialogResources();
        check('copy arrow after sorting: exactly the two selected files', JSON.stringify(resources.slice().sort())
            === '["/home/alice/ui-sort/file10.txt","/home/alice/ui-sort/file2.txt"]', resources);

        await sortBy('age');
        await sortBy('age'); // oldest first
        s = await paneState(LEFT);
        check('still the same two selected when sorted by age', JSON.stringify(s.selected) === '["file2.txt","file10.txt"]', s);
        const right = page.locator('#zone-right-pane .grid-files');
        const box = await right.boundingBox();
        await row('file2.txt').dragTo(right, { targetPosition: { x: 40, y: box.height - 15 } });
        resources = await copyDialogResources();
        check('drag and drop after sorting: exactly the two selected files', JSON.stringify(resources.slice().sort())
            === '["/home/alice/ui-sort/file10.txt","/home/alice/ui-sort/file2.txt"]', resources);

        // Range selection in the displayed order: File1.txt .. file10.txt when sorted by name
        await sortBy('name');
        await row('File1.txt').click();
        await row('file10.txt').click({ modifiers: ['Shift'] });
        s = await paneState(LEFT);
        check('shift-range selection follows the displayed order', JSON.stringify(s.selected) === '["File1.txt","file2.txt","file10.txt"]', s.selected);

        await sortBy('size');
        await sortBy('size');
        await page.reload({ waitUntil: 'domcontentloaded' });
        await page.waitForSelector(`${LEFT} .grid-files`, { timeout: 20000 });
        await leftPane.getByText('ui-sort', { exact: true }).waitFor({ timeout: 20000 });
        s = await paneState(LEFT);
        const rightState = await paneState('#zone-right-pane');
        check('each pane keeps its own sort, also after a reload', s.sort.size === 'descending' && rightState.sort.name === 'ascending',
            [s.sort, rightState.sort]);
        await sortBy('name'); // back to the default for the next flows
    });

    await flow('file-viewer', async () => {
        await openUiSort();
        await row('file2.txt').dblclick();
        await page.waitForSelector('.file-viewer-content', { timeout: 20000 });
        const text = await page.locator('.file-viewer-content').textContent();
        check('double-click on a text file opens the viewer with its content', text === FILE2, text);
        check('viewer: file name and path in the title', (await page.locator('.file-viewer-name').textContent()) === 'file2.txt'
            && (await page.locator('.file-viewer-path').textContent()).includes('/home/alice/ui-sort/file2.txt'));
        check('viewer: rendered as text, not HTML', await page.locator('.file-viewer-content b').count() === 0 && text.includes('<b>not bold</b>'));
        check('viewer: read-only (no inputs, no editable content)', await page.locator('.modal-content textarea, .modal-content input, .modal-content [contenteditable=true]').count() === 0);
        await shot('viewer-text');
        await pageShot('viewer-text-page');
        await page.keyboard.press('Escape');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
        check('viewer closes with Esc', true);

        await row('image.bin').dblclick();
        await page.waitForSelector('.file-viewer-error', { timeout: 20000 });
        const error = await page.locator('.file-viewer-error').textContent();
        check('double-click on a binary file: "not a text file"', error.includes('not a text file'), error);
        await shot('viewer-binary');
        await page.click('.modal-footer button:has-text("Close")');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });

        await row('dira').dblclick();
        await page.waitForSelector(`${LEFT} .grid-files >> text=".."`, { timeout: 20000 });
        await page.waitForFunction(zone => !document.querySelector(zone).textContent.includes('file10.txt'), LEFT, { timeout: 20000 });
        check('double-click on a folder still navigates', await page.locator('.modal-content').count() === 0
            && (await page.textContent('#zone-left-commands')).includes('/home/alice/ui-sort/dira'));

        // ------------------------------------------------ the pager on a ~5 MiB log
        await row('numbered.log').dblclick();
        await page.waitForSelector('.file-viewer-content span', { timeout: 20000 });
        await waitPagerIdle();
        let st = await pagerState();
        check('pager: opening loads only the first chunk (whole lines from line 000001)', st.chunks === 1 && st.first === 1
            && st.last === st.lines && st.lines > 4000 && st.lines < 7000 && st.sequential && st.top === 1, st);
        check('pager: status shows the byte range, size and position', /^Showing bytes 0–262,\d{3} of 5,144,930 · \d+%$/.test(st.status), st.status);
        check('pager: the content has the focus (keys work at once)', st.focused, st);
        check('pager: "Jump to top" and "Jump to bottom" buttons, a key hint',
            await page.locator('.file-viewer-jump-top').count() === 1 && await page.locator('.file-viewer-jump-bottom').count() === 1
            && (await page.locator('.file-viewer-hint').textContent()).includes('Ctrl+End'));
        await shot('viewer-pager-top');

        for (let i = 0; i < 3; i++) {
            await page.keyboard.press('PageDown');
        }
        await page.waitForTimeout(1000); // smooth scrolling
        const paged = await pagerState();
        check('pager: PageDown scrolls the content', paged.scrollTop > paged.clientHeight && paged.top > 50 && paged.chunks === 1, paged);
        // End: the bottom of what is loaded, which loads the next chunk below it
        let before = await chunkSignature();
        await page.keyboard.press('End');
        await waitChunksChanged(before);
        st = await pagerState();
        check('pager: End (the bottom of what is loaded) loads the next chunk, line numbers continue without gaps or duplicates',
            st.chunks === 2 && st.first === 1 && st.last > paged.last && st.sequential && st.lines === st.last, st);
        await shot('viewer-pager-loaded-more');

        // Keep scrolling to the bottom until the end of the file: the top line stays when
        // a chunk is added below and one dropped above; never more than 16 chunks (4 MiB)
        let maxChunks = st.chunks, maxText = st.textLength, allSequential = st.sequential, steps = 0;
        const jumps = [];
        while (!st.eof && steps < 40) {
            before = await chunkSignature();
            const atBottom = await pagerState('end');
            await waitChunksChanged(before);
            st = await pagerState();
            if (st.top !== atBottom.top) {
                jumps.push([atBottom.top, st.top]);
            }
            maxChunks = Math.max(maxChunks, st.chunks);
            maxText = Math.max(maxText, st.textLength);
            allSequential = allSequential && st.sequential;
            steps++;
        }
        check('pager: scrolling reaches "End of file" with the last line', st.eof && st.last === NUMBERED_LINES && st.sequential, st);
        check('pager: memory bound: at most 16 chunks (4 MiB) in the DOM, earlier chunks dropped',
            maxChunks === 16 && maxText <= 16 * 256 * 1024 && st.first > 1 && allSequential, { maxChunks, maxText, first: st.first, steps });
        check('pager: no visible jump while chunks are added below and dropped above', steps >= 15 && jumps.length === 0, { steps, jumps });
        await shot('viewer-pager-scrolled-to-end');

        // Jump to top, then to the bottom with the button: the tail replaces the content
        await page.click('.file-viewer-jump-top');
        await page.waitForFunction(() => document.querySelectorAll('.file-viewer-content span').length === 1
            && document.querySelector('.file-viewer-content span').dataset.offset === '0', null, { timeout: 20000 });
        await waitPagerIdle();
        st = await pagerState();
        check('pager: "Jump to top" shows line 000001 at the top', st.first === 1 && st.top === 1 && st.scrollTop === 0 && st.chunks === 1, st);

        await page.click('.file-viewer-jump-bottom');
        await page.waitForFunction(() => !!document.querySelector('.file-viewer-eof'), null, { timeout: 20000 });
        await waitPagerIdle();
        st = await pagerState();
        check('pager: "Jump to bottom" loads the tail: the last line, visible at the bottom of the viewport',
            st.chunks === 1 && st.last === NUMBERED_LINES && st.bottom === NUMBERED_LINES && st.atBottom && st.first > 1 && st.sequential, st);
        check('pager: status at the end of the file', /^Showing bytes 4,8\d\d,\d{3}–5,144,930 of 5,144,930 · 100%$/.test(st.status), st.status);
        check('pager: the content keeps the focus after a jump', st.focused, st);
        await shot('viewer-pager-bottom');
        await pageShot('viewer-pager-bottom-page');

        // Scroll up: the previous chunk is prepended without a visible jump
        const tailFirst = st.first;
        const up = Math.floor(st.clientHeight / 2); // within the prefetch margin
        const beforePrepend = await pagerState(up);
        await page.waitForFunction(() => document.querySelectorAll('.file-viewer-content span').length === 2, null, { timeout: 20000 });
        await waitPagerIdle();
        st = await pagerState();
        check('pager: scrolling up after a jump loads the earlier lines', st.chunks === 2 && st.first < tailFirst
            && st.last === NUMBERED_LINES && st.sequential, st);
        check('pager: no visible jump when the earlier lines are prepended', beforePrepend.top !== null && st.top === beforePrepend.top
            && st.scrollTop > up + 1000, [beforePrepend.top, st.top, up, st.scrollTop]);
        await shot('viewer-pager-scrolled-up');

        // Ctrl+Home / Ctrl+End: start and end of the file
        await page.keyboard.press('Control+Home');
        await page.waitForFunction(() => document.querySelector('.file-viewer-content span')?.dataset.offset === '0'
            && document.querySelectorAll('.file-viewer-content span').length === 1, null, { timeout: 20000 });
        await waitPagerIdle();
        st = await pagerState();
        check('pager: Ctrl+Home goes to the start of the file', st.top === 1 && st.chunks === 1, st);
        await page.keyboard.press('Control+End');
        await page.waitForFunction(() => !!document.querySelector('.file-viewer-eof'), null, { timeout: 20000 });
        await waitPagerIdle();
        st = await pagerState();
        check('pager: Ctrl+End goes to the end of the file', st.bottom === NUMBERED_LINES && st.atBottom, st);
        check('pager: never more than 16 chunks after jumping around', st.chunks <= 16 && maxChunks <= 16, st);
        await page.keyboard.press('Escape');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
        check('pager: no page errors', pageErrors.length === 0, pageErrors);
    });

    // ------------------------------------------------ follow mode (tail -f) on a growing log
    await flow('file-viewer-follow', async () => {
        let nextLine = FOLLOW_LINES + 1;
        // Appends numbered lines to the log as alice, like a program writing it
        const append = (n = 1) => {
            const lines = [];
            for (let i = 0; i < n; i++) {
                lines.push(`line ${String(nextLine++).padStart(6, '0')} of the followed log`);
            }
            appShell(`sudo -u alice sh -c 'cat >> ${FOLLOW_LOG}'`, lines.join('\n') + '\n');
            return nextLine - 1;
        };
        const chunkRequests = [];
        let inFlight = 0, maxInFlight = 0;
        const isChunk = r => r.url().includes('/api/system/files/view/chunk/');
        page.on('request', r => {
            if (isChunk(r)) {
                chunkRequests.push({ t: Date.now(), body: r.postData() || '' });
                maxInFlight = Math.max(maxInFlight, ++inFlight);
            }
        });
        const done = r => { if (isChunk(r)) inFlight--; };
        page.on('requestfinished', done);
        page.on('requestfailed', done);
        const hasLine = n => page.waitForFunction(n => (document.querySelector('.file-viewer-content') || {}).textContent
            ?.includes(`line ${String(n).padStart(6, '0')} of`), n, { timeout: 15000 });
        const followState = () => page.evaluate(() => ({
            pressed: document.querySelector('.file-viewer-follow')?.getAttribute('aria-pressed'),
            status: document.querySelector('.file-viewer-follow-status')?.textContent || '',
            paused: document.querySelector('.file-viewer-paused')?.textContent || '',
            newLines: Number((document.querySelector('.file-viewer-new-lines')?.textContent || '-1').replace(/\D/g, '')),
            notice: document.querySelector('.file-viewer-notice')?.textContent || '',
        }));

        await openUiSort();
        await row('dirB').dblclick();
        await leftPane.getByText('follow.log', { exact: true }).waitFor({ timeout: 20000 });
        await row('follow.log').dblclick();
        await page.waitForSelector('.file-viewer-content span', { timeout: 20000 });
        await waitPagerIdle();
        check('follow: a "Follow" button next to Jump to top/bottom', await page.locator('.file-viewer-jumps .file-viewer-follow').count() === 1
            && (await followState()).pressed === 'false');

        // F (the content has the focus): the end of the file, polling for new lines
        await page.keyboard.press('Shift+F');
        await page.waitForSelector('.file-viewer-follow-status', { timeout: 10000 });
        let fs = await followState();
        let st = await pagerState();
        check('follow: F turns it on: "Following… (updated …)", the button pressed, at the end of the file',
            fs.pressed === 'true' && /^Following… \(updated (just now|\d+ s ago)\)$/.test(fs.status) && st.atBottom && st.bottom === FOLLOW_LINES, [fs, st]);

        // A program appends lines over several seconds: each shows up within ~5 s, at the bottom
        const delays = [];
        let last = 0;
        for (let i = 0; i < 4; i++) {
            last = append(i + 1);
            const t0 = Date.now();
            await hasLine(last);
            delays.push(Date.now() - t0);
            await page.waitForTimeout(700);
        }
        await page.waitForTimeout(300); // scrolled after the render
        st = await pagerState();
        fs = await followState();
        check('follow: appended lines appear within ~5 s', delays.every(d => d < 5500), delays);
        check('follow: auto-scroll: the newest line is visible at the bottom', st.atBottom && st.bottom === last && st.last === last
            && st.sequential, st);
        check('follow: the appended lines join the last chunk (no chunk per poll)', st.chunks <= 2, st.chunks);
        check('follow: status says when it last got new lines', /^Following… \(updated (just now|[0-5] s ago)\)$/.test(fs.status), fs.status);
        const polls = chunkRequests.filter(r => r.body.includes('"follow":true'));
        check('follow: polls are forward reads with follow=true from the end of what is shown', polls.length >= 3
            && polls.every(r => /"offset":\d+/.test(r.body)), polls.map(r => r.body).slice(0, 3));
        await shot('viewer-follow-following');
        await pageShot('viewer-follow-following-page');

        // Scroll up: auto-scroll pauses, the new lines are still received and counted
        const box = await page.locator('.file-viewer-scroll').boundingBox();
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.mouse.wheel(0, -700);
        await page.waitForSelector('.file-viewer-paused', { timeout: 5000 });
        await page.waitForTimeout(400); // smooth scrolling
        const pausedAt = await pagerState();
        fs = await followState();
        check('follow: scrolling up pauses: "Paused: 0 new lines below · Resume"', /^Paused: 0 new lines below · Resume$/.test(fs.paused)
            && !pausedAt.atBottom && fs.status === 'Following paused', [fs, pausedAt.scrollTop]);
        const counts = [];
        for (let i = 0; i < 3; i++) {
            last = append(2);
            const expected = 2 * (i + 1);
            await page.waitForFunction(n => Number((document.querySelector('.file-viewer-new-lines')?.textContent || '').replace(/\D/g, '')) >= n,
                expected, { timeout: 15000 });
            counts.push((await followState()).newLines);
        }
        st = await pagerState();
        check('follow: while paused, "N new lines below" counts up as lines are appended', JSON.stringify(counts) === '[2,4,6]', counts);
        check('follow: while paused the view stays where it was (no auto-scroll)', Math.abs(st.scrollTop - pausedAt.scrollTop) < 2
            && st.top === pausedAt.top && !st.atBottom, [pausedAt.scrollTop, st.scrollTop, pausedAt.top, st.top]);
        await shot('viewer-follow-paused');
        await pageShot('viewer-follow-paused-page');

        await page.click('.file-viewer-resume');
        await page.waitForSelector('.file-viewer-paused', { state: 'detached', timeout: 10000 });
        await page.waitForTimeout(300);
        st = await pagerState();
        fs = await followState();
        check('follow: Resume jumps to the newest line and follows again', st.atBottom && st.bottom === last
            && fs.status.startsWith('Following…'), [st, fs]);
        last = append(1);
        await hasLine(last);
        await page.waitForTimeout(300);
        st = await pagerState();
        check('follow: after Resume new lines scroll into view again', st.atBottom && st.bottom === last, st);

        // The log is truncated (rotated): a notice, and the new end is shown
        appShell(`sudo -u alice sh -c 'cat > ${FOLLOW_LOG}'`,
            [1, 2, 3].map(i => `line ${String(i).padStart(6, '0')} after the truncation`).join('\n') + '\n');
        await page.waitForSelector('.file-viewer-notice-truncated', { timeout: 15000 });
        await page.waitForFunction(() => (document.querySelector('.file-viewer-content') || {}).textContent
            === 'line 000001 after the truncation\nline 000002 after the truncation\nline 000003 after the truncation\n', null, { timeout: 15000 });
        await page.waitForTimeout(300);
        fs = await followState();
        st = await pagerState();
        check('follow: truncated file: "File was truncated, showing the new end", the tail reloaded', fs.notice.startsWith('File was truncated, showing the new end')
            && st.lines === 3 && st.status.includes('of 99') && fs.status.startsWith('Following…'), [fs, st]);
        await shot('viewer-follow-truncated');
        await pageShot('viewer-follow-truncated-page');
        nextLine = 4;
        last = append(1);
        await hasLine(last);
        check('follow: after the truncation new lines are followed again', (await pagerState()).last === 4);

        // Hidden tab: no polls; visible again: polls at once
        await page.evaluate(() => {
            Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
            document.dispatchEvent(new Event('visibilitychange'));
        });
        await page.waitForTimeout(1000); // an answer on its way
        let before = chunkRequests.length;
        await page.waitForTimeout(5000);
        check('follow: no polls while the tab is hidden', chunkRequests.length === before, chunkRequests.length - before);
        await page.evaluate(() => {
            delete document.hidden;
            document.dispatchEvent(new Event('visibilitychange'));
        });
        await page.waitForTimeout(1000);
        check('follow: polls again as soon as the tab is visible', chunkRequests.length > before, chunkRequests.length - before);

        // The F key toggles it off and on again
        await page.locator('.file-viewer-scroll').focus();
        await page.keyboard.press('f');
        await page.waitForSelector('.file-viewer-follow-status', { state: 'detached', timeout: 5000 });
        fs = await followState();
        check('follow: F again turns it off', fs.pressed === 'false', fs);
        await page.waitForTimeout(1000);
        before = chunkRequests.length;
        await page.waitForTimeout(4500);
        check('follow: off: no more polls', chunkRequests.length === before, chunkRequests.length - before);
        await page.keyboard.press('F');
        await page.waitForSelector('.file-viewer-follow-status', { timeout: 5000 });
        check('follow: F turns it on again', (await followState()).pressed === 'true');

        // Closing the dialog stops polling
        await page.waitForTimeout(2500);
        await page.keyboard.press('Escape');
        await page.waitForSelector('.modal-content', { state: 'detached', timeout: 10000 });
        const closedAt = Date.now();
        await page.waitForTimeout(7000);
        const after = chunkRequests.filter(r => r.t > closedAt);
        check('follow: closing the dialog stops polling (0 requests to /files/view/chunk/ afterwards)', after.length === 0, after);
        check('follow: never two chunk requests at once', maxInFlight === 1, maxInFlight);
        check('follow: no page errors', pageErrors.length === 0, pageErrors);
    });
} else {
    // Callback mode: Microsoft (fake_ms.py) sends the sign-in tab back to Motuz, which
    // opens the dialog there on the "signed in, choose drive" step
    await flow('onedrive-callback', async () => {
        await loginUi();
        await openClouds();
        await newConnection('onedrive');
        checkBeforeSignIn('OneDrive (callback)', await dialog(), 'Sign in with Microsoft');
        const [tab] = await Promise.all([context.waitForEvent('page'), page.click('button:has-text("Sign in with Microsoft")')]);
        tab.on('pageerror', e => pageErrors.push(e.message));
        await page.waitForSelector('text=Motuz continues in that tab', { timeout: 15000 });
        check('OneDrive (callback): this tab says the sign-in continues in the other tab', true);
        await tab.waitForSelector('.modal-content .oauth-account', { timeout: 30000 });
        check('OneDrive (callback): the tab is back at Motuz, address cleaned up', new URL(tab.url()).pathname === '/clouds'
            && !tab.url().includes('oauth_state'), tab.url());
        let d = await dialog(tab);
        check('OneDrive (callback): dialog on OneDrive with the drive picker', d.type === 'onedrive'
            && await tab.locator('.oauth-sign-in select option').count() === 2, d);
        await tab.waitForTimeout(4000);
        d = await dialog(tab);
        check('OneDrive (callback): still on OneDrive with the drive picker after 4 seconds', d.type === 'onedrive'
            && await tab.locator('.oauth-sign-in select option').count() === 2 && d.account.includes('Signed in as Alice Test'), d);
        check('OneDrive (callback): Connection Name prefilled with the drive', d.name === 'OneDrive (Alice Test)', d.name);
        checkOneNameOneCreate('OneDrive (callback)', d);
        checkSignInPanel('OneDrive (callback)', d);
        check('OneDrive (callback): footer is Cancel and Create connection',
            JSON.stringify(footerTexts(d)) === '["Cancel","Create connection"]' && !d.error, d);
        await shot('onedrive-callback-signed-in', tab);
        await tab.fill('input[name=name]', 'ui-onedrive-callback');
        await tab.click('.modal-footer button:has-text("Create connection")');
        await tab.waitForSelector('.modal-content', { state: 'detached', timeout: 15000 });
        await tab.waitForSelector('table >> text="ui-onedrive-callback"', { timeout: 15000 });
        const conn = await connection('ui-onedrive-callback');
        check('OneDrive (callback): connection created with the own app', conn && conn.type === 'onedrive'
            && conn.onedrive_drive_id === 'b!mydrive' && conn.onedrive_client_id === 'motuz-own-app', conn);
        await tab.close();
        await closeDialog();
    });
}

check('no JavaScript errors on the pages', pageErrors.length === 0, pageErrors);
await browser.close();
await api.dispose();
const passed = results.filter(r => r).length;
console.log(`\n${passed}/${results.length} passed`);
process.exit(passed === results.length ? 0 : 1);
