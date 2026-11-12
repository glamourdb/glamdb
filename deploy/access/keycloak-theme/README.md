# GlamDB native login theme

`glamdb/login` extends the pinned Keycloak `keycloak.v2` login theme. Keycloak
continues to render and validate username, password, reset, MFA and required-action
forms. No credential forms, authentication JavaScript or upstream templates are
copied. The account and administrative consoles retain their upstream themes.

Compose mounts this directory and the existing `ui/assets/brand` directory
read-only. The wordmark and mark PNGs are served unchanged by Keycloak. Fonts and
styles are local, and no tracking is added. Keep the brand mount separate from
private configuration and secrets.

New realm bootstrap selects `loginTheme: glamdb`. For an existing realm, mount the
theme, recreate only Keycloak, then select **Realm settings → Themes → Login theme
→ glamdb** in the private native Admin Console. This changes the realm's login
presentation; it does not require reprovisioning a realm, resetting a password,
changing authentication flows, or recreating users and organizations. Do not rerun
the one-time realm provisioner against an existing realm.

## Runtime styling

Edit `glamdb/login/resources/css/tokens.css` for colors, fonts, radii, the mobile
brand gap (`--glamdb-brand-gap`) and the local wordmark URL. `glamdb.css` contains
layout and native PatternFly control overrides.
The default tokens match `ui/config/theme.json`; these are separate files, so an
operator changing the palette should update both. Neither change requires a core,
gateway or Keycloak binary rebuild.

Keep Keycloak's production theme/template caching enabled. After changing static
resources, clear only its generated theme gzip cache and reload the browser:

```sh
dc exec keycloak sh -c 'rm -rf /opt/keycloak/data/tmp/kc-gzip-cache'
```

Browsers may retain their existing cached resources until their cache lifetime
expires; a forced reload fetches the current CSS. Changes to `theme.properties`
or message bundles require recreating Keycloak so its theme metadata is reloaded.
Preserve the PostgreSQL and identity state throughout a recreation. See the
[official theme guide](https://www.keycloak.org/ui-customization/themes) for the
upstream inheritance and caching contract.

## Read-only browser check

Install the existing pinned UI dependencies and Chromium, then run against an
explicit HTTPS deployment with an existing username:

```sh
cd ui
npm ci
npx --no-install playwright install chromium
GLAMDB_LOGIN_SITE=https://workbench.example.com \
GLAMDB_LOGIN_USERNAME=existing-user \
  npx --no-install playwright test --config=playwright.login-theme.config.mjs
```

The check opens desktop and mobile username/password screens, verifies the pink
controls and unchanged wordmark bytes, and checks for horizontal overflow. It
never supplies a password or changes an account. Screenshots contain empty
password fields; the attempted username is masked. Trace, video and automatic
failure screenshots are disabled. Results stay under ignored `artifacts/`.

Also verify one normal sign-in and an unsubmitted native required-action screen
with the operator's existing account before admitting users. Keep credentials
out of screenshots, traces, browser storage, command arguments and test output.
