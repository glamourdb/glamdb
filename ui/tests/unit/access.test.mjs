import test from 'node:test';
import assert from 'node:assert/strict';
import { loadAccessSession, selectDatabase, validateAccessSession } from '../../assets/access.js';

const session = {
    version: 1,
    group: { id: 'pink', name: 'Pink team', login: 'pink-user' },
    databases: [
        { id: 'alpha', name: 'Alpha' },
        { id: 'shared', name: 'Shared' },
    ],
};

test('organization sessions preserve a single normalized authenticated identity', () => {
    const organization = { id: 'acme', name: 'Acme', login: 'acme-login' };
    const result = validateAccessSession({
        version: 2,
        organization,
        databases: session.databases,
    });
    assert.equal(result.version, 2);
    assert.deepEqual(result.organization, organization);
    assert.deepEqual(result.group, organization);
    assert.throws(
        () => validateAccessSession({ version: 2, group: organization, databases: [] }),
        /session/i,
    );
});

test('session validation retains only the group and allowed database identities', () => {
    const input = structuredClone(session);
    input.backend = 'https://elsewhere.example';
    input.token = 'must-not-be-copied';
    input.group.password = 'must-not-be-copied';
    input.databases[0].url = 'https://elsewhere.example';
    const result = validateAccessSession(input);
    assert.deepEqual(result, session);
    input.databases[0].name = 'Changed';
    assert.equal(result.databases[0].name, 'Alpha');
});

test('malformed sessions and unsafe or duplicate identities cannot select a database', () => {
    for (const input of [
        null,
        { ...session, version: 2 },
        { ...session, group: null },
        { ...session, group: { ...session.group, id: '../pink' } },
        { ...session, group: { ...session.group, id: true } },
        { ...session, group: { ...session.group, name: '' } },
        { ...session, group: { ...session.group, login: 4 } },
        { ...session, databases: {} },
        { ...session, databases: [{ id: 'Alpha', name: 'Alpha' }] },
        { ...session, databases: [{ id: ['alpha'], name: 'Alpha' }] },
        { ...session, databases: [{ id: 'alpha/../../beta', name: 'Alpha' }] },
        { ...session, databases: [{ id: 'a'.repeat(65), name: 'Alpha' }] },
        { ...session, databases: [{ id: 'alpha', name: null }] },
        { ...session, databases: [session.databases[0], session.databases[0]] },
    ])
        assert.throws(() => validateAccessSession(input), /session/i);
});

test('the access session is fetched without a script-managed authorization header', async () => {
    let received;
    const controller = new AbortController();
    const result = await loadAccessSession(async (url, options) => {
        received = { url, options };
        return Response.json(session);
    }, controller.signal);
    assert.deepEqual(result, session);
    assert.equal(received.url, '/workbench/session');
    assert.equal(received.options.cache, 'no-store');
    assert.equal(received.options.credentials, 'same-origin');
    assert.equal(received.options.signal, controller.signal);
    assert.equal(new Headers(received.options.headers).has('Authorization'), false);
    await assert.rejects(
        loadAccessSession(async () => new Response('Unavailable', { status: 503 })),
        /503/,
    );
    await assert.rejects(
        loadAccessSession(async () => Response.json({})),
        /session/i,
    );
});

test('switching databases aborts every request and removes all previous workspace data', async () => {
    const theme = { brand: { name: 'GlamDB' } };
    let finish;
    const first = selectDatabase({ theme, epoch: 3 }, session, 'alpha', {
        origin: 'https://workbench.example',
        fetchImpl: async () => new Promise((resolve) => (finish = resolve)),
    });
    const pending = first.api.json('/v0/collections');
    Object.assign(first, {
        capabilities: {},
        collections: [{ name: 'alpha-only' }],
        collectionId: 'same-id',
        snapshotId: 'old-snapshot',
        resolvedSnapshot: 'old-snapshot',
        collection: {},
        schema: { fields: [{ name: 'private-column' }] },
        stats: {},
        snapshots: [{}],
        files: [{}],
        rows: [{ key: 'private-row' }],
        result: {},
        plan: {},
        selectedFile: 'private-file',
        lastQuery: {},
        clauses: [{}],
        projection: ['private-column'],
        operations: [{}],
        mutation: {},
        creation: {},
        tab: 'data',
        page: 3,
    });
    const controllers = Array.from({ length: 3 }, () => new AbortController());
    let request;
    const second = selectDatabase(first, session, 'shared', {
        origin: 'https://workbench.example',
        controllers,
        fetchImpl: async (url, options) => {
            request = { url, options };
            return Response.json({ collections: [] });
        },
    });
    for (const controller of controllers) assert.equal(controller.signal.aborted, true);
    for (const key of [
        'capabilities',
        'collectionId',
        'snapshotId',
        'resolvedSnapshot',
        'collection',
        'schema',
        'stats',
        'result',
        'plan',
        'selectedFile',
        'lastQuery',
        'mutation',
        'creation',
    ])
        assert.equal(second[key], null, key);
    for (const key of [
        'collections',
        'snapshots',
        'files',
        'rows',
        'clauses',
        'projection',
        'operations',
    ])
        assert.deepEqual(second[key], [], key);
    assert.equal(second.theme, theme);
    assert.equal(second.epoch, 5);
    assert.equal(second.page, 0);
    assert.equal(second.tab, 'overview');
    assert.equal(second.token, '');
    assert.equal(second.databaseId, 'shared');
    assert.equal(second.base, 'https://workbench.example/databases/shared');
    assert.deepEqual(second.access, session);
    assert.notEqual(second.api, first.api);
    finish(Response.json({ collections: [{ name: 'alpha-only' }] }));
    await assert.rejects(pending, { name: 'AbortError' });
    await second.api.json('/v0/collections');
    assert.equal(request.url, 'https://workbench.example/databases/shared/v0/collections');
    assert.equal(new Headers(request.options.headers).has('Authorization'), false);
    assert.deepEqual(second.collections, []);
});

test('an unlisted database cannot be opened and an empty allowlist creates no client', () => {
    assert.throws(
        () => selectDatabase({}, session, 'beta', { origin: 'https://workbench.example' }),
        /access/i,
    );
    const empty = selectDatabase(
        { rows: [{ key: 'old' }], api: { close() {} }, epoch: 1 },
        { ...session, databases: [] },
        null,
        { origin: 'https://workbench.example' },
    );
    assert.equal(empty.api, null);
    assert.equal(empty.databaseId, null);
    assert.deepEqual(empty.rows, []);
    assert.deepEqual(empty.collections, []);
    assert.deepEqual(empty.access.databases, []);
});
