import { expect, test } from '@playwright/test';

test.use({ trace: 'off', screenshot: 'off', video: 'off' });

async function groupPage(browser, group) {
    const context = await browser.newContext({
        baseURL: process.env.GLAMDB_GROUP_TEST_URL,
        ignoreHTTPSErrors: true,
        httpCredentials: {
            username: process.env[`GLAMDB_GROUP_TEST_${group}_USER`],
            password: process.env[`GLAMDB_GROUP_TEST_${group}_PASSWORD`],
        },
        viewport: { width: 1440, height: 1000 },
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
        const fetch = window.fetch;
        window.accessRequests = [];
        window.fetch = (input, options) => {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname === '/workbench/session' || url.pathname.startsWith('/databases/'))
                window.accessRequests.push({
                    path: url.pathname,
                    authorization: new Headers(options?.headers).has('Authorization'),
                    credentials: options?.credentials,
                    cache: options?.cache,
                });
            return fetch(input, options);
        };
    });
    return { context, page };
}

for (const [group, name, database, collection, row, allowed, foreign] of [
    [
        'PINK',
        'Pink team',
        'alpha',
        'alpha_only',
        'alpha-row',
        ['Alpha', 'Shared', 'Offline'],
        'beta_only',
    ],
    ['BLUE', 'Blue team', 'beta', 'beta_only', 'beta-row', ['Beta', 'Shared'], 'alpha_only'],
]) {
    test(`${group.toLowerCase()} group sees its allowed databases and routed data`, async ({
        browser,
    }) => {
        const { context, page } = await groupPage(browser, group);
        try {
            await page.goto('/');
            await expect(page.locator('.workspace-tag')).toContainText(name);
            await expect(page.locator('#database-selector')).toHaveValue(database);
            await expect(page.locator('#database-selector option')).toHaveText(allowed);
            await expect(page.locator('.breadcrumb strong')).toHaveText(collection);
            await expect(page.locator('.collection-list')).not.toContainText(foreign);
            await expect(page.getByLabel('Access token')).toHaveCount(0);
            await expect(page.getByLabel('Server URL')).toHaveCount(0);
            await page
                .locator('nav')
                .getByRole('button', { name: 'Data studio', exact: true })
                .click();
            await page.getByRole('button', { name: 'Run query', exact: true }).click();
            await expect(page.getByRole('table', { name: 'Query results' })).toContainText(row);
            await page.locator('#database-selector').selectOption('shared');
            await expect(page.locator('.breadcrumb strong')).toHaveText('common');
            await expect(page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
            await page
                .locator('nav')
                .getByRole('button', { name: 'Data studio', exact: true })
                .click();
            await page.getByRole('button', { name: 'Run query', exact: true }).click();
            await expect(page.getByRole('table', { name: 'Query results' })).toContainText(
                'shared-row',
            );
            await expect(page.getByRole('table', { name: 'Query results' })).not.toContainText(row);
            expect(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).toEqual({});
            const requests = await page.evaluate(() => window.accessRequests);
            expect(requests.length).toBeGreaterThan(0);
            expect(requests.every((request) => !request.authorization)).toBe(true);
            expect(requests.every((request) => request.credentials === 'same-origin')).toBe(true);
            expect(requests.every((request) => request.cache === 'no-store')).toBe(true);
        } finally {
            await context.close();
        }
    });
}

test('a late query cannot restore data or dialogs from the previous database', async ({
    browser,
}) => {
    const { context, page } = await groupPage(browser, 'PINK');
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    let started;
    const waiting = new Promise((resolve) => (started = resolve));
    let finished;
    const done = new Promise((resolve) => (finished = resolve));
    try {
        await page.goto('/');
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        await page.route('**/databases/alpha/v0/collections/*/scan', async (route) => {
            const response = await route.fetch();
            started();
            await gate;
            try {
                await route.fulfill({ response });
            } catch {
                // A canceled browser request cannot receive its delayed response.
            } finally {
                finished();
            }
        });
        await page.locator('nav').getByRole('button', { name: 'Data studio', exact: true }).click();
        await page.getByRole('button', { name: 'Run query', exact: true }).click();
        await waiting;
        await page.getByRole('button', { name: 'Append rows', exact: true }).click();
        await expect(page.locator('#modal')).toBeVisible();
        await page.locator('#modal').evaluate((modal) => modal.close());
        await page.locator('#database-selector').selectOption('shared');
        await expect(page.locator('.breadcrumb strong')).toHaveText('common');
        release();
        await done;
        await expect(page.locator('#modal')).not.toBeVisible();
        await expect(page.locator('#modal')).toBeEmpty();
        await expect(page.locator('#view')).not.toContainText('alpha-row');
        await page.locator('nav').getByRole('button', { name: 'Data studio', exact: true }).click();
        await expect(page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
        await expect(page.locator('#snapshot-selector')).toHaveValue('latest');
        await page.getByRole('button', { name: 'Run query', exact: true }).click();
        await expect(page.getByRole('table', { name: 'Query results' })).toContainText(
            'shared-row',
        );
        await expect(page.getByRole('table', { name: 'Query results' })).not.toContainText(
            'alpha-row',
        );
        await page.locator('nav').getByRole('button', { name: 'Operations', exact: true }).click();
        await expect(page.locator('.operations-card')).toHaveCount(0);
    } finally {
        release();
        await context.close();
    }
});

test('branding and pink runtime styling remain available in the group workspace', async ({
    browser,
}) => {
    const { context, page } = await groupPage(browser, 'PINK');
    try {
        await page.goto('/');
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        for (const selector of ['.brand-wordmark', '.brand-mark']) {
            await expect(page.locator(selector)).toBeVisible();
            await expect
                .poll(() =>
                    page
                        .locator(selector)
                        .evaluate((node) => node.complete && node.naturalWidth > 0),
                )
                .toBe(true);
        }
        await page
            .locator('nav')
            .getByRole('button', { name: 'Style studio', exact: true })
            .click();
        await page.getByLabel('Color accent', { exact: true }).fill('#ed0878');
        await page.getByLabel('Color accent', { exact: true }).dispatchEvent('change');
        await expect
            .poll(() =>
                page.evaluate(() =>
                    getComputedStyle(document.documentElement)
                        .getPropertyValue('--color-accent')
                        .trim(),
                ),
            )
            .toBe('#ed0878');
        await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
        await expect(page.getByRole('heading', { name: 'Your databases' })).toBeVisible();
        await expect(page.locator('#app')).not.toContainText('Pink team');
        await expect(page.getByText(/does not clear the browser's saved sign-in/)).toBeVisible();
        await page.getByRole('button', { name: 'Reload access', exact: true }).click();
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
    } finally {
        await context.close();
    }
});

test('an empty access list shows no database or simulated workspace', async ({ browser }) => {
    const { context, page } = await groupPage(browser, 'EMPTY');
    const databaseRequests = [];
    page.on('request', (request) => {
        if (new URL(request.url()).pathname.startsWith('/databases/'))
            databaseRequests.push(request.url());
    });
    try {
        await page.goto('/');
        await expect(page.getByRole('heading', { name: 'Your databases' })).toBeVisible();
        await expect(page.getByText('No databases are available for this group.')).toBeVisible();
        await expect(page.locator('#database-selector')).toHaveCount(0);
        await expect(page.locator('.collection-list')).toHaveCount(0);
        await expect(page.getByLabel('Access token')).toHaveCount(0);
        expect(databaseRequests).toEqual([]);
    } finally {
        await context.close();
    }
});

test('an unreachable database shows an error and allows another database selection', async ({
    browser,
}) => {
    const { context, page } = await groupPage(browser, 'PINK');
    try {
        await page.goto('/');
        await expect(page.locator('.breadcrumb strong')).toHaveText('alpha_only');
        await page.locator('#database-selector').selectOption('offline');
        await expect(page.getByRole('heading', { name: 'Database unavailable' })).toBeVisible();
        await expect(page.locator('#database-selector')).toHaveValue('offline');
        await expect(page.locator('#database-selector option')).toHaveText([
            'Alpha',
            'Shared',
            'Offline',
        ]);
        await expect(page.locator('.collection-list')).toBeEmpty();
        await expect(page.locator('#view')).not.toContainText('alpha_only');
        await expect(page.getByRole('table', { name: 'Query results' })).toHaveCount(0);
        await expect(
            page.getByRole('button', { name: 'New collection', exact: true }),
        ).toBeDisabled();
        await page.locator('#database-selector').selectOption('shared');
        await expect(page.locator('.breadcrumb strong')).toHaveText('common');
        await expect(page.getByRole('heading', { name: 'Database unavailable' })).toHaveCount(0);
    } finally {
        await context.close();
    }
});
