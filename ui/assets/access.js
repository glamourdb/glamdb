import { WorkbenchAPI } from './api.js';
import { resetConnection } from './model.js';

const ID = /^[a-z][a-z0-9_-]{0,63}$/;
const text = (value) => typeof value === 'string' && value.trim().length > 0;

export function validateAccessSession(value) {
    const organization = value?.version === 2;
    const group = organization ? value.organization : value?.group;
    if (
        ![1, 2].includes(value?.version) ||
        typeof group?.id !== 'string' ||
        !ID.test(group.id) ||
        !text(group?.name) ||
        !text(group?.login) ||
        !Array.isArray(value.databases)
    )
        throw new Error('The access session is invalid');
    const seen = new Set();
    const databases = value.databases.map((database) => {
        if (
            typeof database?.id !== 'string' ||
            !ID.test(database.id) ||
            !text(database?.name) ||
            seen.has(database.id)
        )
            throw new Error('The access session is invalid');
        seen.add(database.id);
        return { id: database.id, name: database.name };
    });
    return {
        version: value.version,
        group: { id: group.id, name: group.name, login: group.login },
        ...(organization
            ? { organization: { id: group.id, name: group.name, login: group.login } }
            : {}),
        databases,
    };
}

export async function loadAccessSession(fetchImpl = globalThis.fetch.bind(globalThis), signal) {
    const response = await fetchImpl('/workbench/session', {
        cache: 'no-store',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        signal,
    });
    if (!response.ok) throw new Error(`Access session is unavailable (${response.status})`);
    return validateAccessSession(await response.json());
}

export function selectDatabase(
    state,
    access,
    databaseId,
    { origin = globalThis.location.origin, fetchImpl, controllers = [] } = {},
) {
    access = validateAccessSession(access);
    if (databaseId !== null && !access.databases.some((database) => database.id === databaseId))
        throw new Error('This database is not in your access list');
    const base = new URL(origin).origin;
    const next = {
        ...resetConnection(state),
        base: databaseId === null ? base : `${base}/databases/${encodeURIComponent(databaseId)}`,
        access,
        databaseId,
        databaseLoading: false,
        databaseError: '',
    };
    if (databaseId !== null)
        next.api = new WorkbenchAPI(next.base, '', fetchImpl, undefined, { gateway: true });
    state.api?.close?.();
    for (const controller of controllers) controller?.abort();
    return next;
}
