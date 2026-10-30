# Security policy

Report suspected vulnerabilities privately using
[GitHub private vulnerability reporting](https://github.com/curlewlabs-com/rules_archive_store/security/advisories/new).
Include the affected source revision or release, the executor and host, the
reproduction steps, and the observed impact. Avoid credentials and private
archive contents in public issues and CI logs.

Curlew Labs maintains this repository; Jeffrey Wall
([jeffwall-curlewlabs](https://github.com/jeffwall-curlewlabs)) is the release
and security contact through GitHub. Reports are reviewed on a best-effort
basis without a guaranteed response time. Fixes target the latest published
release; older release lines have no promised backport window.

## Trust boundary

The conversion refuses an archive whose digest differs from the declared one,
and any member or link that would land outside its tree or beneath one of the
archive's own links, judged as a case-insensitive macOS volume judges names. The
mount helper extracts only tars the conversion wrote, and refuses an expansion
holding a symlink that resolves outside it. Those checks keep a declared
archive's contents inside its tree; they do not make an untrusted archive's
programs safe to run.

The store is shared by every action an executor instance runs. Its trees are
read-only against accident, not against intent: an action can restore a write
bit on a tree its account expanded, and every action account shares the
store's group. Actions on one instance are trusted with each other's trees, as
a shared build directory already trusts them. Do not run builds you do not
trust on an instance whose store other builds read.
