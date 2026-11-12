import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

const username = process.env.GLAMDB_LOGIN_USERNAME;
test.skip(
    !process.env.GLAMDB_LOGIN_SITE || !username,
    'Explicit HTTPS deployment and existing username are required; no passwords are submitted',
);

// Keep automatic failure context free of transient authentication URLs.
test.afterEach(async ({ page }) => {
    await page.goto('about:blank').catch(() => {});
});

async function checkTheme(page) {
    const accent = await page.evaluate(() =>
        getComputedStyle(document.documentElement).getPropertyValue('--glamdb-accent').trim(),
    );
    expect(accent).toBe('#c41472');
    await expect(page.locator('#kc-login')).toHaveCSS('background-color', 'rgb(196, 20, 114)');
    await expect(page.locator('#kc-login')).toHaveCSS('border-radius', '12px');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    expect(overflow).toBe(false);
    if (page.viewportSize().width <= 760) {
        const gap = await page.evaluate(
            () =>
                document.querySelector('.pf-v5-c-login__main').getBoundingClientRect().top -
                document.querySelector('#kc-header').getBoundingClientRect().bottom,
        );
        expect(gap).toBeGreaterThanOrEqual(16);
    }
    const image = await page
        .locator('.kc-logo-text')
        .evaluate((element) => getComputedStyle(element).backgroundImage.slice(5, -2));
    expect(new URL(image).origin).toBe(new URL(page.url()).origin);
    expect(new URL(image).pathname).toContain('/login/glamdb/');
    const response = await page.request.get(image);
    expect(response.status()).toBe(200);
    const original = await readFile(
        new URL('../../assets/brand/wordmark-transparent.png', import.meta.url),
    );
    const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
    expect(digest(await response.body())).toBe(digest(original));
}

for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
]) {
    test.describe(`${viewport.width}px login`, () => {
        test.use({ viewport });
        test('native username and password forms retain the pink theme', async ({ page }, info) => {
            await page.goto('/');
            await page.locator('input[name="username"]').waitFor({ state: 'visible' });
            expect(new URL(page.url()).protocol).toBe('https:');
            await checkTheme(page);
            await page.screenshot({ path: info.outputPath('username.png'), fullPage: true });
            await page.locator('input[name="username"]').fill(username);
            if (!(await page.locator('input[name="password"]').isVisible())) {
                await page.locator('#kc-login').click();
            }
            await expect(page.locator('input[name="password"]')).toBeVisible();
            await expect(page.locator('input[name="password"]')).toHaveValue('');
            await checkTheme(page);
            await page.screenshot({
                path: info.outputPath('password.png'),
                fullPage: true,
                mask: [
                    page.locator('#kc-attempted-username'),
                    page.locator('input[name="username"]'),
                ],
                maskColor: '#fce0ef',
            });
        });
    });
}
