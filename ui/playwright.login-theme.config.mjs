import { defineConfig } from '@playwright/test';

const site = new URL(process.env.GLAMDB_LOGIN_SITE ?? 'https://localhost');
if (
    site.protocol !== 'https:' ||
    site.username ||
    site.password ||
    site.search ||
    site.hash ||
    site.pathname !== '/'
) {
    throw new Error('GLAMDB_LOGIN_SITE must be an HTTPS origin without credentials or query');
}

export default defineConfig({
    testDir: './tests/e2e',
    testMatch: 'login-theme.spec.mjs',
    workers: 1,
    retries: 0,
    timeout: 45000,
    reporter: 'list',
    outputDir: '../artifacts/login-theme',
    use: {
        baseURL: site.origin,
        ignoreHTTPSErrors: false,
        trace: 'off',
        screenshot: 'off',
        video: 'off',
        launchOptions: process.env.GLAMDB_BROWSER_PATH
            ? { executablePath: process.env.GLAMDB_BROWSER_PATH }
            : {},
    },
});
