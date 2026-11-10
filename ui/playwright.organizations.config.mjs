import { defineConfig } from '@playwright/test';
import { isAbsolute } from 'node:path';

const baseURL = process.env.GLAMDB_GROUP_TEST_URL;
if (!baseURL || !/^https:\/\/127\.0\.0\.1:\d+$/.test(baseURL))
    throw new Error('Organization access tests require an explicit local HTTPS test server');
for (const organization of ['PINK', 'BLUE', 'EMPTY']) {
    if (
        !process.env[`GLAMDB_GROUP_TEST_${organization}_USER`] ||
        !process.env[`GLAMDB_GROUP_TEST_${organization}_PASSWORD`]
    )
        throw new Error('Disposable organization test credentials are required');
}
for (const name of ['GLAMDB_ORGANIZATION_TEST_SOCKET', 'GLAMDB_ORGANIZATION_TEST_PYTHON']) {
    if (!process.env[name] || !isAbsolute(process.env[name]))
        throw new Error('Organization access tests require explicit local operator paths');
}

export default defineConfig({
    testDir: './tests/protected-demo',
    testMatch: 'organizations.spec.mjs',
    timeout: 60000,
    expect: { timeout: 15000 },
    workers: 1,
    retries: 0,
    reporter: 'list',
    outputDir: '../artifacts/organization-access-browser',
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
