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
const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 1300 } });
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
    for (const path of ['/home/alice/ui-src/sub', '/home/alice/ui-dst']) {
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
