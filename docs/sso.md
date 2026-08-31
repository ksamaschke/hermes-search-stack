# Single sign-on (OIDC)

Open WebUI supports OIDC out of the box. The stack ships **without** it so a
fresh clone works with local accounts; you turn it on by creating one extra
Secret.

Works with any OIDC provider — Keycloak, Authentik, Authelia, Zitadel, Okta,
Entra ID, Google.

## 1. Create a client in your IdP

Create a **confidential** client (one that has a client secret):

- **Client ID:** `open-webui` (anything you like)
- **Client authentication:** on
- **Standard flow:** enabled
- **Valid redirect URI:** `https://<your-open-webui-host>/oauth/oidc/callback`
- **Web origin:** `https://<your-open-webui-host>`

The callback path is fixed by Open WebUI: `/oauth/oidc/callback`. A mismatch
here is the single most common cause of a failed login.

## 2. Create the Secret

Open WebUI reads its OIDC settings from environment variables, and the
deployment pulls them in with `envFrom` + `optional: true`:

```bash
kubectl -n hermes-search create secret generic open-webui-oidc \
  --from-literal=ENABLE_OAUTH_SIGNUP=true \
  --from-literal=OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true \
  --from-literal=OAUTH_PROVIDER_NAME="My SSO" \
  --from-literal=OAUTH_CLIENT_ID=open-webui \
  --from-literal=OAUTH_CLIENT_SECRET='<client secret>' \
  --from-literal=OPENID_PROVIDER_URL='https://idp.example.com/realms/myrealm/.well-known/openid-configuration' \
  --from-literal=OPENID_REDIRECT_URI='https://chat.example.com/oauth/oidc/callback' \
  --from-literal=OAUTH_SCOPES='openid email profile'
```

Then restart:

```bash
kubectl -n hermes-search rollout restart deploy/open-webui
```

## Key details

**`OPENID_PROVIDER_URL` must be the full discovery document URL**, ending in
`/.well-known/openid-configuration` — not the issuer root. Open WebUI does not
append it for you.

**`ENABLE_OAUTH_SIGNUP=true`** lets a successful SSO login create the account
on first use. Without it, only users who already exist locally can sign in.

**`OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`** links an SSO identity to an existing
local account with the same email address. Convenient when migrating an
existing instance, but it means your IdP's email claim is trusted — only
enable it if the IdP verifies email ownership.

**The first user to sign up becomes the admin.** Everyone after that lands in
`pending` until an admin promotes them. To change that:
`--from-literal=DEFAULT_USER_ROLE=user`.

## Verifying

```bash
# discovery document reachable from inside the cluster
kubectl -n hermes-search exec deploy/open-webui -- \
  curl -fsS "$OPENID_PROVIDER_URL" | head -c 200

# the login page should now show an SSO button
curl -s https://chat.example.com/ | grep -io 'oauth[^"]*'
```

Watch the logs during a login attempt:

```bash
kubectl -n hermes-search logs -f deploy/open-webui | grep -i oauth
```

## Hermes WebUI

The admin UI has **no OIDC support** — it authenticates with a single
password (`HERMES_WEBUI_PASSWORD`). Since it can run arbitrary commands as the
agent, put it behind one of:

- a VPN or an internal-only hostname (simplest, recommended)
- an authenticating reverse proxy (oauth2-proxy, Authelia, Contour + an
  ext_authz filter)

Do not rely on its password alone on a public hostname.
