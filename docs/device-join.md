# Join an existing mesh without copying its token

Run `swarph join <gateway> --name <new-peer>`. This is new-identity onboarding,
not recovery or rotation: existing names are refused. The newcomer needs no
mesh credential. Ask the operator to approve the displayed code:

```
swarph mesh approve ABCD-EFGH --gateway <gateway> --witness <ratified-peer> --token-file <operator-token-file>
```

The operator previews the name, source address and expiry, verifies the code
came from the intended client, and types `yes`. A source address is advisory,
not proof of identity. `--refuse` replaces `--witness` for an explicit refusal.
Only the gateway's existing operator authority can decide; an ordinary lab
peer token is not an operator credential. No newcomer token reaches this CLI.

The client polls with its private device secret. Approval expires 15 minutes
after the original request, and pickup returns the peer token once. Competing
pending requests do not reserve a name: approving one refuses the others.
Refused, expired or consumed requests cannot be approved again.

The client stores its token exclusively at
`~/.config/swarph/<name>.peer_token` (0600, directory 0700). Existing files and
symlinked credential directories are refused before a request. The separate
`<name>.join.json` records only name, gateway and token path; it does not alter
operator.json, the environment, or default identity. Use the explicit
`--as`, `--gateway` and `--token-file` command printed on success.

After `/whoami` confirms the identity, interactive clients collect four
own-words answers and send them to the operator's chosen witness. Without
interactive input, the questions are printed and no answers are fabricated
or sent: send your own answers with `swarph mesh send <witness> --content-file
<answers> --as <name> --gateway <gateway> --token-file <private-token-file>`.
Registration remains **unratified** until the existing witness process runs.
Do not put credentials into those answers or any mesh message.

HTTPS is supported. Plain HTTP is accepted without an unsafe flag only for
literal `100.64.0.0/10` addresses or `*.ts.net` hosts; the CLI explicitly says
this relies on the tailnet's encrypted tunnel, not TLS. Other cleartext HTTP
requires `--allow-unsafe-http` and is unsafe for real credentials. No redirects
or environment HTTP proxies are used. An unreachable URL suggests checking
connectivity/tailnet membership; the CLI cannot join the tailnet for you.

The secret stays in client memory until pickup and is sent only in request
bodies. It is never printed or put into argv/environment. The gateway stores
hashes, not raw secrets. Both responses and validation errors are non-cacheable.
The token is never replayed: losing a pickup response after the server commits,
or failing to save it, can strand the generation. Stop and ask an operator to
use the existing revoke/recovery process; do not loop or overwrite an existing
identity. This build does not automate that recovery or install/restart anything.

Native Windows credential storage is not supported; use Linux/WSL for `join`.
Operator preview/decision is a separate read-only-credential client action.
Local processes that can read your private files or memory can still steal
your credential; device onboarding does not replace per-cell OS isolation.
