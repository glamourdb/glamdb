import { expect, test } from '@playwright/test';

test('anonymous and incorrect credentials cannot read the page or a direct asset', async ({
    playwright,
    baseURL,
}) => {
    for (const httpCredentials of [
        undefined,
        { username: process.env.GLAMDB_PROTECTED_TEST_USER, password: 'wrong-test-only' },
    ]) {
        const client = await playwright.request.newContext({
            baseURL,
            ignoreHTTPSErrors: true,
            httpCredentials,
        });
        try {
            for (const path of ['/', '/assets/app.js', '/config/demo-data.json']) {
                const response = await client.get(path);
                expect(response.status()).toBe(401);
                expect(response.headers()['www-authenticate']).toMatch(/^Basic /);
                expect(response.headers()['cache-control']).toBe('private, no-store');
            }
        } finally {
            await client.dispose();
        }
    }
});

test('authenticated browser loads logos, every page and live pink styling', async ({ page }) => {
    const errors = [],
        apiRequests = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
        if (new URL(request.url()).pathname.startsWith('/v0/')) apiRequests.push(request.url());
    });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'A little overview' })).toBeVisible();
    for (const selector of ['.brand-wordmark', '.brand-mark']) {
        const logo = page.locator(selector);
        await expect(logo).toBeVisible();
        await expect
            .poll(() => logo.evaluate((node) => node.complete && node.naturalWidth > 0))
            .toBe(true);
    }
    for (const name of [
        'Data studio',
        'Schema',
        'Snapshots',
        'Storage',
        'Explain',
        'Operations',
        'Style studio',
    ]) {
        await page.locator('nav').getByRole('button', { name, exact: true }).click();
        await expect(page.locator('#view h1')).toBeVisible();
    }
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
    expect(await page.evaluate(() => ({ ...localStorage, ...sessionStorage }))).toEqual({});
    expect(errors).toEqual([]);
    expect(apiRequests).toEqual([]);
});

test('shared website password keeps each visitor data isolated', async ({
    page,
    browser,
    baseURL,
}) => {
    await page.goto('/');
    await page.getByRole('button', { name: 'New collection', exact: true }).click();
    await page.getByLabel('Collection name').fill('authenticated_visitor');
    await page.getByRole('button', { name: 'Create collection' }).click();
    await expect(page.locator('.breadcrumb strong')).toHaveText('authenticated_visitor');
    const otherContext = await browser.newContext({
        baseURL,
        ignoreHTTPSErrors: true,
        httpCredentials: {
            username: process.env.GLAMDB_PROTECTED_TEST_USER,
            password: process.env.GLAMDB_PROTECTED_TEST_PASSWORD,
        },
    });
    try {
        const other = await otherContext.newPage();
        await other.goto('/');
        await expect(other.getByRole('heading', { name: 'A little overview' })).toBeVisible();
        await expect(other.locator('.collection-list')).not.toContainText('authenticated_visitor');
    } finally {
        await otherContext.close();
    }
    await page.reload();
    await expect(page.locator('.collection-list')).not.toContainText('authenticated_visitor');
});
