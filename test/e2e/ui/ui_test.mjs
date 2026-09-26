// Playwright UI checks against the e2e stack (https://localhost), run by test/e2e/run.sh
// after the credentials suite (the picker checks use the files it leaves in alice's home)
// and while the app uses rclone's OneDrive app (paste mode).
// Chromium: playwright's own (npx playwright install chromium), or MOTUZ_E2E_CHROMIUM.
// Screenshots of failed flows go to $MOTUZ_E2E_LOGS (default test/e2e/logs).
import { chromium, request } from 'playwright';
import { mkdirSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const BASE = process.env.MOTUZ_E2E_BASE || 'https://localhost';
const LOGS = process.env.MOTUZ_E2E_LOGS || join(dirname(fileURLToPath(import.meta.url)), '..', 'logs');
mkdirSync(LOGS, { recursive: true });

const results = [];
function check(name, ok, detail = '') {
    results.push(!!ok);
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : ' ' + JSON.stringify(detail)}`);
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
        await page.screenshot({ path: join(LOGS, `ui-${name.replace(/\W+/g, '-')}.png`) }).catch(() => {});
        await page.keyboard.press('Escape').catch(() => {});
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

// Fixtures through the API: two folders in alice's home for the drag and drop copy
const api = await request.newContext({ baseURL: BASE, ignoreHTTPSErrors: true });
const login = await (await api.post('/api/auth/login/', { data: { username: 'alice', password: 'AlicePass1' } })).json();
const auth = { Authorization: 'Bearer ' + login.access };
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
    // Microsoft (fake_ms.py) redirects to rclone's localhost address, where nothing listens
    let redirected = '';
    const listener = r => { if (r.url().startsWith('http://localhost:53682/')) redirected = r.url(); };
    context.on('request', listener);
    const [popup] = await Promise.all([context.waitForEvent('page'), page.click('button:has-text("Sign in with Microsoft")')]);
    for (let i = 0; i < 50 && !redirected; i++) await page.waitForTimeout(200);
    context.off('request', listener);
    check('sign-in popup ends at the rclone redirect with code and state', redirected.includes('code=') && redirected.includes('state='), redirected);
    await popup.close();
    await page.fill('textarea', redirected);
    await page.click('button:has-text("Continue")');
    await page.waitForSelector('text=Signed in', { timeout: 20000 });
    const options = await page.$$eval('.card select option', os => os.map(o => o.textContent));
    check('drives offered', options.some(o => o.includes('OneDrive (Alice Test)')) && options.some(o => o.includes('Lab Share - Documents')), options);
    await page.click('button:has-text("Create OneDrive connection")');
    await page.waitForSelector('.modal-content', { state: 'detached', timeout: 15000 });
    await page.waitForSelector('table >> text=OneDrive (Alice Test)', { timeout: 15000 });
    check('OneDrive connection created and listed', true);
});

await flow('credentials-picker', async () => {
    const responses = [];
    const listener = async r => { if (r.url().includes('/local-credentials/')) responses.push(await r.text().catch(() => '')); };
    page.on('response', listener);
    await openClouds();
    await newConnection('s3');
    const picker = page.locator('.local-credentials');
    await picker.waitFor({ timeout: 15000 });
    check('S3: "Found in your home directory" shown', (await picker.locator('h5').innerText()).includes('Found in your home directory'));
    const options = await picker.locator('select option').allInnerTexts();
    const disabled = await picker.locator('select option[disabled]').allInnerTexts();
    check('S3: profiles and rclone remotes offered', options.some(o => o.startsWith('AWS profile: e2e-static'))
        && options.some(o => o.startsWith('rclone remote: s3remote')), options);
    check('S3: MFA role and credential_process profiles disabled', disabled.some(o => o.includes('role-mfa')) && disabled.some(o => o.includes('proc')), disabled);
    const value = await picker.locator('select option', { hasText: 'AWS profile: e2e-static' }).getAttribute('value');
    await picker.locator('select').selectOption(value);
    await page.waitForTimeout(300);
    check('S3: selecting a profile hides the key fields and fills the region',
        await page.locator('input[name=s3_access_key_id]').count() === 0
        && await page.locator('input[name=s3_region]').inputValue() === 'us-west-2'
        && await page.locator('input[type=hidden][name=profile_name]').getAttribute('value') === 'e2e-static');

    await page.selectOption('select[name=type]', 'azureblob');
    await page.locator('.local-credentials select').waitFor({ timeout: 15000 });
    const azure = page.locator('.local-credentials select option', { hasText: 'rclone remote: azurite' });
    check('Azure: the Azurite rclone remote offered', await azure.count() === 1);
    await page.locator('.local-credentials select').selectOption(await azure.getAttribute('value'));
    await page.fill('input[name=name]', 'ui-azurite');
    await page.fill('input[name=bucket]', 'motuztest');
    await page.click('button:has-text("Verify Connection")');
    await page.waitForSelector('button:has-text("Correct"), button:has-text("Incorrect")', { timeout: 60000 });
    check('Azure: Verify Connection is Correct', await page.locator('button:has-text("Correct")').count() === 1);
    await page.click('button:has-text("Create Cloud Connection")');
    await page.waitForSelector('.modal-content', { state: 'detached', timeout: 15000 });
    await page.waitForSelector('table >> text=ui-azurite', { timeout: 15000 });
    check('Azure: connection created and listed', true);

    await newConnection('webdav');
    await page.waitForTimeout(500);
    check('WebDAV: no credentials picker', await page.locator('.local-credentials').count() === 0);
    await page.keyboard.press('Escape');
    page.off('response', listener);
    // Metadata only: masked key ids, no Azure account key (Azurite's well-known dev key)
    const profiles = responses.flatMap(r => { try { return JSON.parse(r).profiles || []; } catch (e) { return []; } });
    check('local-credentials responses: masked key ids, no account key', profiles.length > 0
        && profiles.every(p => !p.access_key_id || p.access_key_id.startsWith('****'))
        && responses.every(r => !r.includes('Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6')), profiles.map(p => p.access_key_id));
});

check('no JavaScript errors on the pages', pageErrors.length === 0, pageErrors);
await browser.close();
await api.dispose();
const passed = results.filter(r => r).length;
console.log(`\n${passed}/${results.length} passed`);
process.exit(passed === results.length ? 0 : 1);
