import { expect, test } from '@playwright/test';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

test.use({ trace: 'off', screenshot: 'off', video: 'off' });

const run = promisify(execFile);
const root = fileURLToPath(new URL('../../../', import.meta.url));

async function administer(...command) {
    const { stdout } = await run(
        process.env.GLAMDB_ORGANIZATION_TEST_PYTHON,
        [
            '-m',
            'apps.workbench_access.admin',
            '--socket',
            process.env.GLAMDB_ORGANIZATION_TEST_SOCKET,
            ...command,
        ],
        { cwd: root, timeout: 45000, encoding: 'utf8' },
    );
    const result = JSON.parse(stdout);
    expect(result.applied).toBe(true);
    return result;
}

async function organizationPage(browser, organization) {
    const context = await browser.newContext({
        baseURL: process.env.GLAMDB_GROUP_TEST_URL,
        ignoreHTTPSErrors: true,
        httpCredentials: {
            username: process.env[`GLAMDB_GROUP_TEST_${organization}_USER`],
            password: process.env[`GLAMDB_GROUP_TEST_${organization}_PASSWORD`],
        },
        viewport: { width: 1440, height: 1000 },
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.addInitScript(() => {
        window.organizationPageMarker = crypto.randomUUID();
    });
    return { context, page, errors };
}

const optionValues = (page) =>
    page
        .locator('#database-selector option')
        .evaluateAll((options) => options.map((node) => node.value));
const dataStudio = (page) =>
    page.locator('nav').getByRole('button', { name: 'Data studio', exact: true }).click();
async function query(page) {
    await dataStudio(page);
    await page.getByRole('button', { name: 'Run query', exact: true }).click();
}

test('fresh organization contexts expose only their owned databases and no HTTP administration', async ({
    browser,
}) => {
    for (const [organization, name, database, collection, row, allowed, foreign] of [
        [
            'PINK',
            'Pink organization',
            'alpha',
            'alpha_only',
            'alpha-row',
            ['alpha', 'offline', 'shared'],
            'beta-row',
        ],
        ['BLUE', 'Blue organization', 'beta', 'beta_only', 'beta-row', ['beta'], 'alpha-row'],
        ['EMPTY', 'Empty organization', null, null, null, [], 'alpha-row'],
    ]) {
        const { context, page, errors } = await organizationPage(browser, organization);
        try {
            await page.goto('/');
            const sessionResponse = await context.request.get('/workbench/session');
            expect(sessionResponse.status()).toBe(200);
            const session = await sessionResponse.json();
            expect(session.version).toBe(2);
            expect(session.organization.name).toBe(name);
            expect(session.databases.map((item) => item.id)).toEqual(allowed);
            if (database) {
                await expect(page.locator('.workspace-tag')).toContainText(name);
                await expect(page.locator('#database-selector')).toHaveValue(database);
                expect(await optionValues(page)).toEqual(allowed);
                await expect(page.locator('.breadcrumb strong')).toHaveText(collection);
                await query(page);
                await expect(page.getByRole('table', { name: 'Query results' })).toContainText(row);
                await expect(page.getByRole('table', { name: 'Query results' })).not.toContainText(
                    foreign,
                );
            } else {
                await expect(page.getByRole('heading', { name: 'Your databases' })).toBeVisible();
                await expect(page.locator('#app')).toContainText(name);
                await expect(
                    page.getByText('No databases are available for this organization.'),
                ).toBeVisible();
                await expect(page.locator('.shell')).toHaveCount(0);
                await expect(page.locator('#database-selector')).toHaveCount(0);
                await expect(page.locator('#app')).not.toContainText(foreign);
            }
            const denied = await context.request.post('/admin', {
                data: { action: 'organization.disable', params: { id: 'blue' } },
            });
            expect(denied.status()).toBe(404);
            await expect(page.getByLabel('Access token')).toHaveCount(0);
            await expect(page.getByLabel('Server URL')).toHaveCount(0);
            expect(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).toEqual({});
            expect(errors).toEqual([]);
        } finally {
            await context.close();
        }
    }
});

test('an ownership revision removes old data and dialogs without reloading either organization', async ({
    browser,
}) => {
    const pink = await organizationPage(browser, 'PINK');
    const blue = await organizationPage(browser, 'BLUE');
    let transferred = false;
    try {
        await pink.page.goto('/');
        await blue.page.goto('/');
        await expect(pink.page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        await expect(blue.page.locator('.breadcrumb strong')).toHaveText('beta_only');
        const pinkMarker = await pink.page.evaluate(() => window.organizationPageMarker);
        const blueMarker = await blue.page.evaluate(() => window.organizationPageMarker);
        await query(pink.page);
        await expect(pink.page.getByRole('table', { name: 'Query results' })).toContainText(
            'alpha-row',
        );
        await pink.page.evaluate(async () => {
            const { WorkbenchAPI } = await import('/assets/api.js');
            const original = WorkbenchAPI.prototype.json;
            const held = new Promise((resolve) => (window.releaseOrganizationScan = resolve));
            window.organizationScanReady = false;
            window.organizationScanFinished = false;
            WorkbenchAPI.prototype.json = async function (path, options) {
                const hold = this.base.endsWith('/databases/alpha') && path.endsWith('/scan');
                const result = await original.call(this, path, options);
                if (hold) {
                    window.organizationScanReady = true;
                    await held;
                    window.organizationScanFinished = true;
                }
                return result;
            };
        });
        await pink.page.getByLabel('Result limit').fill('1');
        await pink.page.getByRole('button', { name: 'Run query', exact: true }).click();
        await expect.poll(() => pink.page.evaluate(() => window.organizationScanReady)).toBe(true);
        await pink.page.getByRole('button', { name: 'Append rows', exact: true }).click();
        await pink.page
            .getByLabel('Rows · JSON array')
            .fill('[{"key":"unsubmitted-alpha-draft","n":"9"}]');
        await expect(pink.page.locator('#modal')).toBeVisible();

        await administer('database', 'transfer', 'alpha', '--from', 'pink', '--to', 'blue');
        transferred = true;
        await expect.poll(() => optionValues(pink.page)).toEqual(['offline', 'shared']);
        await expect.poll(() => optionValues(blue.page)).toEqual(['alpha', 'beta']);
        await expect(pink.page.locator('#modal')).not.toBeVisible();
        await expect(pink.page.locator('#modal')).toBeEmpty();
        await expect(pink.page.locator('#app')).not.toContainText('alpha_only');
        await expect(pink.page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
        expect(await pink.page.evaluate(() => window.organizationScanFinished)).toBe(false);
        await pink.page.evaluate(() => window.releaseOrganizationScan());
        await expect
            .poll(() => pink.page.evaluate(() => window.organizationScanFinished))
            .toBe(true);
        await expect(pink.page.locator('#app')).not.toContainText('alpha-row');
        await expect(pink.page.locator('#toast')).toBeEmpty();
        expect(await pink.page.evaluate(() => window.organizationPageMarker)).toBe(pinkMarker);
        expect(await blue.page.evaluate(() => window.organizationPageMarker)).toBe(blueMarker);

        await pink.page.locator('#database-selector').selectOption('shared');
        await expect(pink.page.locator('.breadcrumb strong')).toHaveText('common');
        await dataStudio(pink.page);
        await expect(pink.page.getByLabel('Result limit')).toHaveValue('1000');
        await pink.page.getByRole('button', { name: 'Run query', exact: true }).click();
        await expect(pink.page.getByRole('table', { name: 'Query results' })).toContainText(
            'shared-row',
        );
        await expect(pink.page.getByRole('table', { name: 'Query results' })).not.toContainText(
            'alpha-row',
        );
        await blue.page.locator('#database-selector').selectOption('alpha');
        await expect(blue.page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        await query(blue.page);
        await expect(blue.page.getByRole('table', { name: 'Query results' })).toContainText(
            'alpha-row',
        );
        await expect(blue.page.getByRole('table', { name: 'Query results' })).not.toContainText(
            'shared-row',
        );
        expect(pink.errors).toEqual([]);
        expect(blue.errors).toEqual([]);
    } finally {
        await pink.page.evaluate(() => window.releaseOrganizationScan?.()).catch(() => {});
        await pink.context.close();
        await blue.context.close();
        if (transferred)
            await administer('database', 'transfer', 'alpha', '--from', 'blue', '--to', 'pink');
    }
});

test('organization revocation clears an open workspace and re-enabling permits a fresh selection', async ({
    browser,
}) => {
    const { context, page, errors } = await organizationPage(browser, 'PINK');
    let disabled = false;
    try {
        await page.goto('/');
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        const marker = await page.evaluate(() => window.organizationPageMarker);
        await query(page);
        await expect(page.getByRole('table', { name: 'Query results' })).toContainText('alpha-row');
        await page.getByRole('button', { name: 'Append rows', exact: true }).click();
        await expect(page.locator('#modal')).toBeVisible();

        await administer('organization', 'disable', 'pink');
        disabled = true;
        await expect(page.getByRole('heading', { name: 'Your databases' })).toBeVisible();
        await expect(page.locator('#login-error')).toContainText('no longer authorized');
        await expect(page.locator('.shell')).toHaveCount(0);
        await expect(page.locator('#database-selector')).toHaveCount(0);
        await expect(page.locator('.collection-list')).toHaveCount(0);
        await expect(page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
        await expect(page.locator('#modal')).not.toBeVisible();
        await expect(page.locator('#modal')).toBeEmpty();
        await expect(page.locator('#app')).not.toContainText('alpha-row');
        await expect(page.locator('#app')).not.toContainText('Pink organization');
        expect(await page.evaluate(() => window.organizationPageMarker)).toBe(marker);
        expect(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).toEqual({});

        await administer('organization', 'enable', 'pink');
        disabled = false;
        await page.getByRole('button', { name: 'Reload access', exact: true }).click();
        await expect(page.locator('.workspace-tag')).toContainText('Pink organization');
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        await expect(page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
        expect(await page.evaluate(() => window.organizationPageMarker)).toBe(marker);
        expect(errors).toEqual([]);
    } finally {
        await context.close();
        if (disabled) await administer('organization', 'enable', 'pink');
    }
});
