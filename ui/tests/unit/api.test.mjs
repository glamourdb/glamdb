import test from 'node:test';
import assert from 'node:assert/strict';
import { WorkbenchAPI, APIError } from '../../assets/api.js';

test('token is a header, never part of URL, and HTTP errors preserve code', async () => {
    let received;
    const client = new WorkbenchAPI(
        'http://localhost:8765',
        'private-token',
        async (url, options) => {
            received = { url, options };
            return new Response(
                JSON.stringify({ error: { code: 'NO_SUCH_COLLECTION', message: 'Missing' } }),
                { status: 404 },
            );
        },
    );
    await assert.rejects(
        client.json('/v0/collections/x'),
        (error) => error instanceof APIError && error.code === 'NO_SUCH_COLLECTION',
    );
    assert.equal(received.options.headers.Authorization, 'Bearer private-token');
    assert.ok(!received.url.includes('private-token'));
});
test('unknown outcome remains unresolved even with HTTP503', async () => {
    const client = new WorkbenchAPI(
        'http://localhost',
        'token',
        async () =>
            new Response(JSON.stringify({ outcome: 'commit_unknown', recovery_required: true }), {
                status: 503,
            }),
    );
    const result = await client.json('/v0/collections/a/append', {
        method: 'POST',
        body: { rows: [] },
        allowOutcome: true,
    });
    assert.equal(result.outcome, 'commit_unknown');
    assert.equal(result.recovery_required, true);
});
test('oversized and malformed responses are errors, never empty success', async () => {
    const large = new WorkbenchAPI(
        'http://localhost',
        'token',
        async () => new Response('x'.repeat(32)),
        8,
    );
    await assert.rejects(large.json('/v0/capabilities'), /too large/i);
    const invalid = new WorkbenchAPI(
        'http://localhost',
        'token',
        async () => new Response('not-json'),
    );
    await assert.rejects(invalid.json('/v0/capabilities'), /JSON/);
});
test('credentials in base URL and insecure nonlocal HTTP are rejected', () => {
    assert.throws(() => new WorkbenchAPI('https://user:password@example.com', 't'));
    assert.throws(() => new WorkbenchAPI('http://example.com', 't'));
});
