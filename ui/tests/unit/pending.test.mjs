import test from 'node:test';
import assert from 'node:assert/strict';
import * as pending from '../../assets/model.js';

const scope = {
    server_url: 'http://localhost:8766',
    database_id: '00000000-0000-4000-8000-000000000001',
    store_id: '00000000-0000-4000-8000-000000000002',
    backend: 'local',
    mock: false,
};
const collectionId = '00000000-0000-4000-8000-000000000003';
const operationId = '00000000-0000-4000-8000-000000000004';
const schema = {
    fields: [
        { field_id: 1, name: 'key', type: 'string', nullable: false },
        { field_id: 2, name: 'n', type: 'int64', nullable: true },
    ],
};
const body = {
    rows: [
        { key: 'same', n: '9007199254740993' },
        { key: 'same', n: null },
    ],
    expected_snapshot: '9007199254740995',
};
const draft = {
    operationId,
    collectionId,
    schema,
    body,
    fingerprint: JSON.stringify(body),
    terminal: false,
    token: 'do-not-export-token',
    result: { token: 'do-not-export-token' },
};

test('pending append export roundtrip retains exact UUID, schema, rows and precondition without credentials', () => {
    assert.equal(typeof pending.exportPending, 'function', 'pending exports must be available');
    const exported = pending.exportPending(scope, 'append', draft);
    const saved = JSON.stringify(exported);
    assert.ok(!saved.includes('do-not-export-token'));
    const imported = pending.importPending(JSON.parse(saved), scope);
    assert.equal(imported.kind, 'append');
    assert.equal(imported.draft.operationId, operationId);
    assert.equal(imported.draft.collectionId, collectionId);
    assert.deepEqual(imported.draft.schema, schema);
    assert.deepEqual(imported.draft.body, body);
    assert.equal(imported.draft.fingerprint, JSON.stringify(body));
    assert.equal(imported.draft.terminal, false);
    assert.equal(imported.draft.outcome, 'unresolved');
});

test('pending create export retains exact collection ID, name and schema for explicit retry', () => {
    assert.equal(typeof pending.exportPending, 'function');
    const body = { collection_id: collectionId, name: 'original_name', schema };
    const exported = pending.exportPending(scope, 'create', { body, token: 'never-export' });
    const imported = pending.importPending(exported, scope);
    assert.equal(imported.kind, 'create');
    assert.deepEqual(imported.draft.body, body);
    assert.equal(imported.draft.fingerprint, JSON.stringify({ name: body.name, schema }));
    assert.ok(!JSON.stringify(exported).includes('never-export'));
});

test('pending import refuses a different database, store or backend rather than reassigning an ID', () => {
    assert.equal(typeof pending.exportPending, 'function');
    const exported = pending.exportPending(scope, 'append', draft);
    for (const changed of [
        { database_id: '00000000-0000-4000-8000-000000000099' },
        { store_id: '00000000-0000-4000-8000-000000000099' },
        { mock: true, backend: 'mock' },
        { server_url: 'http://localhost:8769' },
    ])
        assert.throws(
            () => pending.importPending(exported, { ...scope, ...changed }),
            /different|match/i,
        );
});

test('malformed or altered pending inputs never create replacement UUIDs', () => {
    assert.equal(typeof pending.exportPending, 'function');
    const exported = pending.exportPending(scope, 'append', draft);
    for (const change of [
        (value) => delete value.request.operation_id,
        (value) => (value.request.operation_id = 'not-a-uuid'),
        (value) => (value.request.body.rows[0].n = 9007199254740993),
        (value) => (value.request.body.expected_snapshot = 'latest'),
        (value) => (value.request.body.extra = true),
        (value) => (value.request.token = 'forbidden'),
        (value) => (value.version = 2),
    ]) {
        const invalid = structuredClone(exported);
        change(invalid);
        assert.throws(() => pending.importPending(invalid, scope));
    }
    assert.throws(
        () => pending.exportPending(scope, 'append', { ...draft, terminal: true }),
        /terminal|pending/i,
    );
});

test('pending schema preserves supported UTF-8 field names without imposing identifier syntax', () => {
    const namedSchema = {
        fields: [{ field_id: 1, name: 'длинное имя / value', type: 'string', nullable: false }],
    };
    const exported = pending.exportPending(scope, 'append', {
        ...draft,
        schema: namedSchema,
        body: { rows: [{ 'длинное имя / value': 'retained' }] },
    });
    assert.deepEqual(pending.importPending(exported, scope).draft.schema, namedSchema);
});
