# Security policy

Report suspected vulnerabilities privately using
[GitHub private vulnerability reporting](https://github.com/curlewlabs-com/rules_bun/security/advisories/new).
Include the affected source revision or release, acquisition host and runtime
versions, reproduction steps, and the observed impact. Avoid credentials,
private package contents, or personal data in public issues and CI logs.

Curlew Labs maintains this repository; Jeffrey Wall
([jeffwall-curlewlabs](https://github.com/jeffwall-curlewlabs)) is the release
and security contact through GitHub. Reports are reviewed on a best-effort
basis without a guaranteed response time. Fixes target the latest published
release; before the first release, report against current source. Older release
lines have no promised backport window. Report vulnerabilities in Bun, Node,
or an npm package to that project's maintainers as well when appropriate.

## Trust boundary

Repository acquisition can download packages and execute lifecycle programs.
It is not sandboxed. Its private HOME, temporary directory, and package cache
avoid ordinary installation-state collisions; they do not prevent a malicious
program from accessing the host through absolute paths or the network.

Use verified runtime downloads and reviewed manifests, lockfiles, and lifecycle
inputs. Do not pass secrets in declared installation inputs: those files are
exported with the acquired workspace. Authenticated private-registry support
and credential redaction are outside the current API.

A consumer must verify and restore a trusted closure and supply its own
execution isolation, network policy, and qualified runtimes. Digests in the
closure do not authenticate the manifest or make arbitrary restore code safe
for untrusted paths. The [consumer example](examples/consumer/README.md) is an
example for trusted input, not a security boundary.
