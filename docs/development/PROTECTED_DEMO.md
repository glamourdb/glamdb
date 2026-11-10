# Password-protected browser demo

For organization credentials and database ownership, use the
[organization deployment](ORGANIZATIONS.md). The deployment below is the
separate browser-only demo with a single shared password.

This deployment serves the existing browser-only demo behind **server-side
HTTP Basic authentication over HTTPS**. The browser asks for username `glamdb`
(configurable) and a shared password-token. Every visitor with that token may
enter. HTML, JavaScript, images and sample/theme JSON all require authentication.
There is no password check or embedded secret in frontend JavaScript.

This is an optional deployment, not an update to the existing anonymous
https://nnrpi.github.io/glamdb/ publication. GitHub Pages is static hosting and
cannot run this authentication server. Merging the code alone does not protect
that URL. A new HTTPS host must be deployed first; retire the anonymous Pages
publication when switching. Leaving a public origin as an alternate URL would
defeat access restriction. Already downloaded files cannot be revoked.

## Scope

- Caddy checks the shared username/password before serving any demo resource.
  Missing/incorrect credentials return 401, including direct asset requests.
- Plain HTTP only redirects to HTTPS. Responses use `private, no-store`, HSTS,
  `nosniff`, `no-referrer` and frame denial. No request/access logging is enabled;
  Caddy's administration API and configuration autosave are disabled.
- The password is supplied only to the browser's native authentication prompt.
  The server receives a bcrypt hash through its process environment, outside
  the served directory. The launcher requires cost 12–16; use 14 normally.
- The application still uses isolated tab-local BrowserDemoAPI data, not shared
  or durable database storage. This gate does not implement the native database
  HTTP API, SDK bearer authorization, user accounts or per-user permissions.

## Install and configure

Use a host with Python 3.9+ and Caddy, a domain pointing to that host, and TCP
ports 80/443 available. Automatic public certificate issuance requires valid
DNS and reachable challenge ports. A continuously running service/process is
required; GitHub Pages cannot execute it.

From a source checkout:

```sh
python3 scripts/fetch-demo-caddy.py
```

The installer supports Linux AMD64 and macOS ARM64. It downloads the official
Caddy 2.11.9 archive, verifies its pinned SHA-512 and extracts only the regular
executable to ignored `artifacts/tools/caddy-2.11.9/caddy`. It does not install
system software. Other hosts may supply a reviewed Caddy binary with `--caddy`.
Keep Caddy updated through reviewed version/checksum changes.

Generate a unique token with a password manager: at least 32 random bytes
(for example, 43 URL-safe characters), shorter than bcrypt's 72-byte limit.
Do not reuse the database API token. Hash it interactively without putting the
plaintext in shell history or command arguments:

```sh
export GLAMDB_DEMO_HOST=demo.example.com
export GLAMDB_DEMO_USER=glamdb
export GLAMDB_DEMO_PASSWORD_HASH="$(artifacts/tools/caddy-2.11.9/caddy hash-password --algorithm bcrypt --bcrypt-cost 14)"
```

Enter the chosen token at the terminal prompt; input is not echoed in a TTY.
Keep the plaintext in the password manager and share it privately with visitors.
Never commit real environment files, hashes, plaintext tokens or TLS keys.
For a service, provision these three variables through the host's secret manager
or a root-readable environment file outside this checkout and the served root.
Do not paste secrets into a PR, CI variables for public tests or issue comments.

Start the protected site:

```sh
python3 scripts/serve-protected-demo.py --caddy "$PWD/artifacts/tools/caddy-2.11.9/caddy"
```

The launcher first rejects missing or invalid configuration. It packages only
the public UI allowlist into `artifacts/protected-demo/site`, then replaces its
process with Caddy. Caddy serves those files directly; there is no second
unprotected HTTP file server. TLS state is kept in the separate private
`artifacts/protected-demo/state` directory. Preserve this directory across service
restarts for certificate renewal. Run under a dedicated service account and use
the host's normal mechanism to allow it to bind ports 80/443. Supervise the
process with the host's service manager; a development terminal is not hosting.

For an existing certificate, provide both `--tls-cert /outside/site/cert.pem`
and `--tls-key /outside/site/key.pem`. Certificate/key paths must be outside
the served directory. No plaintext HTTP serving mode exists. Test scripts use
disposable certificates and do not install a CA into the machine's trust store.

## Verify and switch publication

1. Check the new HTTPS hostname without credentials: `/`, `/assets/app.js` and
   `/config/demo-data.json` must all return 401 with a Basic challenge.
2. Open the hostname in a private browser window. Enter the configured username
   and token in the browser prompt. Verify logos, navigation and Style studio.
   Wrong credentials must not open any page or direct asset URL.
3. Replace public links with the verified new hostname and remove/disable the
   anonymous Pages publication. Verify the old URL no longer serves the demo.
   Do not remove the existing publication before the protected replacement works.

The shared token can be revoked only for everyone at once: generate a new token,
replace `GLAMDB_DEMO_PASSWORD_HASH` in the hosting secret configuration and restart
the process. Old credentials then receive 401. Browsers cache Basic credentials;
there is no application logout button. Close **all** windows of the private
browser session to discard its credentials. No account-specific revocation,
MFA, brute-force throttling or audit trail is provided by this small demo gate.

## Tests

With the existing workbench dependencies and Chromium installed:

```sh
python3 scripts/fetch-demo-caddy.py
bash scripts/check-protected-demo.sh
```

This explicitly requires a real Caddy binary; absence fails instead of skipping
or starting a mock. It tests trusted/untrusted HTTPS, anonymous/wrong/correct
credentials, direct assets, HTTP redirects, private paths, no credential leakage
and rotation. Playwright verifies the packaged UI behind that same HTTPS process,
including every page, logos, live styling and separate visitor state. Disposable
test credentials are generated in memory. Authentication headers are not retained
in browser traces/HAR. Local test-only certificate bypass in Chromium is bounded
to `https://127.0.0.1:<port>`; HTTPX separately verifies CA trust and rejection.

`GLAMDB_DEMO_CADDY` selects another test binary. `GLAMDB_BROWSER_PATH` uses the
existing Playwright executable override. The ordinary workbench suite remains
separate and unchanged; CI executes both suites with no production secrets.

Official references: [GitHub Pages static hosting](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages),
[Caddy basic_auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth),
[automatic HTTPS](https://caddyserver.com/docs/automatic-https) and
[password hashing](https://caddyserver.com/docs/command-line#caddy-hash-password).
