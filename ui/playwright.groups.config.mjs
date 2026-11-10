import { defineConfig } from '@playwright/test';

const baseURL = process.env.GLAMDB_GROUP_TEST_URL;
if (!baseURL || !/^https:\/\/127\.0\.0\.1:\d+$/.test(baseURL)) {
    throw new Error('Group access tests require an explicit local HTTPS test server');
}
for (const group of ['PINK', 'BLUE', 'EMPTY']) {
    if (
        !process.env[`GLAMDB_GROUP_TEST_${group}_USER`] ||
        !process.env[`GLAMDB_GROUP_TEST_${group}_PASSWORD`]
    )
        throw new Error('Disposable group test credentials are required');
}

export default defineConfig({
    testDir: './tests/protected-demo',
    testMatch: 'groups.spec.mjs',
    timeout: 30000,
    expect: { timeout: 7000 },
    workers: 1,
    retries: 0,
    reporter: 'list',
    outputDir: '../artifacts/group-access-browser',
    use: {
        baseURL,
        ignoreHTTPSErrors: true,
        trace: 'off',
        screenshot: 'off',
        video: 'off',
        launchOptions: process.env.GLAMDB_BROWSER_PATH
            ? { executablePath: process.env.GLAMDB_BROWSER_PATH }
            : {},
    },
});
