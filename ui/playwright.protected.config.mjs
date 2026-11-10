import { defineConfig } from '@playwright/test';

const baseURL = process.env.GLAMDB_PROTECTED_TEST_URL;
if (!baseURL || !/^https:\/\/127\.0\.0\.1:\d+$/.test(baseURL)) {
    throw new Error('Protected demo tests require an explicit local HTTPS test server');
}
if (!process.env.GLAMDB_PROTECTED_TEST_USER || !process.env.GLAMDB_PROTECTED_TEST_PASSWORD) {
    throw new Error('Disposable protected-demo test credentials are required');
}

export default defineConfig({
    testDir: './tests/protected-demo',
    timeout: 30000,
    expect: { timeout: 7000 },
    workers: 1,
    retries: 0,
    reporter: 'list',
    outputDir: '../artifacts/protected-demo-browser',
    use: {
        baseURL,
        // Only the disposable, local test CA; production TLS is never bypassed.
        ignoreHTTPSErrors: true,
        httpCredentials: {
            username: process.env.GLAMDB_PROTECTED_TEST_USER,
            password: process.env.GLAMDB_PROTECTED_TEST_PASSWORD,
        },
        viewport: { width: 1440, height: 1000 },
        // Authentication headers must not be retained in traces or HAR files.
        trace: 'off',
        screenshot: 'only-on-failure',
        launchOptions: process.env.GLAMDB_BROWSER_PATH
            ? { executablePath: process.env.GLAMDB_BROWSER_PATH }
            : {},
    },
});
