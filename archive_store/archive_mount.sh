#!/bin/sh
# Mount declared archives into an action's input root, then run its command.
#
# Usage: archive_mount.sh [--root DIR] MANIFEST -- COMMAND [ARG...]
#
# A toolchain, SDK or dependency tree is declared to Bazel as its archive
# (docs/store.md). This helper turns each archive the manifest
# names into a directory the command reads in place: it expands the archive
# once into the worker instance's store, keyed by its digest, and
# links the mount path in the working directory to that expansion.
#
# This is POSIX sh over system utilities because it runs inside every action
# that reads an archive: a declared tree it needed would be laid out in every
# one of them, or, as an archive, would need this helper to expand it first.
# Absolute system shells and utilities are therefore dependencies of every
# executor that runs one.
#
# MANIFEST holds one mount per line, fields separated by `|`:
#
#   MOUNT|DIGEST|ARCHIVE|STRIP
#
# ARCHIVE, and a DIGEST file, are relative to the working directory. MOUNT is
# too, or with --root relative to DIR, which a test passes: its working
# directory is its runfiles tree, where the archives are, while its mounts
# belong in its own temporary directory (archives.bzl's archive_test). With --root the
# command finds DIR, made absolute, in ARCHIVE_MOUNT_ROOT, and still runs in
# the working directory it was given. ARCHIVE is the directory of parts of a
# tar declared_archive's conversion wrote (archive_to_tar.py), which checked
# every member's path and link before the tree existed; ArchiveInfo, which
# names it here, is created by nothing else (archives.bzl). The parts join, in
# name order, into the one tar DIGEST names.
# DIGEST is the archive's lowercase hex SHA-256, or `@PATH` naming a file whose first line is
# that digest. STRIP is the directory inside the archive to mount, or `.` for
# its root. MOUNT and STRIP are normalized relative paths, no mount may sit
# inside another, compared without regard to case since a macOS volume ignores
# it, and no directory above a mount may be a symlink.
#
# An expansion's key is DIGEST, inside a namespace named for this helper's own
# digest. The helper hashes an archive when it expands it, never when it reuses
# an expansion, since hashing a large archive on every action would cost what
# the store saves. What binds DIGEST to the archive's bytes on every action is
# the conversion, which writes the digest of the tar it wrote, and Bazel caches
# it per archive version.
#
# RULES_ARCHIVE_STORE_DIR names the store. Whatever sets it does so outside the
# action key, as a Buildbarn worker's per-runner environmentVariables do, and
# empties the store before its instance starts: nothing here flushes an
# expansion to disk before publishing it, so one a crash interrupted may be
# published but truncated. Unset, or when the helper cannot claim its namespace
# in the store (see claim_namespace), it expands into a private store that it
# removes when the command exits.

set -eu

caller_path=${PATH:-}
caller_umask=$(umask)
# The helper's own commands come from the system, whatever PATH the action has.
PATH=/usr/bin:/bin
export PATH
# A CDPATH would send the helper's relative `cd -P` calls, and those of the
# shells it starts, somewhere else; the command gets it back.
caller_has_cdpath=${CDPATH+set}
caller_cdpath=${CDPATH-}
unset CDPATH

log() {
    printf 'archive_mount: %s\n' "$*" >&2
}

fail() {
    log "error: $*"
    exit 1
}

sha256_of() {
    shasum -a 256 "$1" | sed 's/ .*//'
}

# The digest a DIGEST field names: itself, or the first word of the first line
# of the file `@PATH` names, which reads shasum's own output form too.
digest_value() {
    case $1 in
    @*)
        value=
        IFS= read -r value <"${1#@}" || [ -n "$value" ] ||
            fail "cannot read a digest from ${1#@}"
        value=${value%% *}
        ;;
    *) value=$1 ;;
    esac
    case $value in
    *[!0-9a-f]* | "") fail "digest '$value' is not lowercase hex" ;;
    esac
    [ "${#value}" -eq 64 ] || fail "digest '$value' is not SHA-256"
    printf '%s\n' "$value"
}

# Refuse a path that could leave the directory it is relative to, or that names
# its target with empty or `.` components.
check_relative() {
    case /$2/ in
    //* | */./* | */../* | *//*)
        fail "$1 '$2' is not a normalized relative path"
        ;;
    esac
}

# Every slot's account shares the store through the action group. The caller's
# umask comes back before the command runs, so its outputs are made as they
# would be without this helper.
umask 002

usage="usage: archive_mount.sh [--root DIR] MANIFEST -- COMMAND [ARG...]"
# Prepended to each mount path: empty, or the absolute --root and a slash.
mount_root=
if [ "${1:-}" = "--root" ]; then
    [ "$#" -ge 2 ] || fail "$usage"
    mkdir -p "$2"
    ARCHIVE_MOUNT_ROOT=$(cd -P "$2" && pwd -P)
    export ARCHIVE_MOUNT_ROOT
    mount_root=$ARCHIVE_MOUNT_ROOT/
    shift 2
fi
[ "$#" -ge 3 ] || fail "$usage"
manifest=$1
[ "$2" = "--" ] || fail "$usage"
shift 2
[ -f "$manifest" ] || fail "manifest $manifest is not a file"

discard() {
    # Expansions are read-only, so their directories must be made writable
    # before their entries can be removed. By absolute path: the private
    # store's removal runs after the caller's PATH is back, which an action
    # may set empty.
    /bin/chmod -R u+w "$1" 2>/dev/null || true
    /bin/rm -rf "$1"
}

private_store=
# Expand into a store of this process's own, removed when it exits.
use_private_store() {
    store=$(mktemp -d "${TMPDIR:-/tmp}/archive-store.XXXXXX")
    store=$(cd -P "$store" && pwd -P)
    private_store=$store
    trap 'discard "$private_store"' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

if [ -n "${RULES_ARCHIVE_STORE_DIR:-}" ]; then
    store=$RULES_ARCHIVE_STORE_DIR
    [ -d "$store" ] || fail "RULES_ARCHIVE_STORE_DIR=$store is not a directory"
    # Absolute, since a mount links to it from a directory of its own.
    store=$(cd -P "$store" && pwd -P)
else
    use_private_store
    log "no RULES_ARCHIVE_STORE_DIR; expanding into private store $store"
fi

# Who this process is, as an evictor of the store tells it apart from others:
# its PID and start time, which `ps` prints the same way on Linux and macOS
# given the C locale and UTC. In store mode the helper execs the command, which
# keeps its PID and start time, so what this process names lives exactly as
# long as the action's root process. An evictor reads this identity out of the
# names below and must match it against `ps` again, so a PID reissued to
# another process does not vouch for it. `lstart` resolves to a second, so a
# PID reissued within the second its first holder started in still reads as
# that holder; a match only ever keeps a tree, so the residual costs disk until
# the store is next emptied, never a tree a reader needed. A finer start time
# would need macOS's kinfo_proc read by struct offset, which ps does not
# expose and no stable interface describes.
started_at=$(LC_ALL=C TZ=UTC0 ps -o lstart= -p "$$" | tr -cd '0-9A-Za-z')
[ -n "$started_at" ] || fail "cannot read this process's start time from ps"
identity=$$-$started_at

# Expansions are namespaced by this helper's own digest, so a change to how it
# extracts or checks a tree never reuses one an earlier revision made; the
# first actions after a helper change expand afresh.
namespace_name=$(sha256_of "$0" | cut -c1-16)

# An evictor retires a namespace nothing is in by marking it `.retiring`,
# reading it again, and only then renaming it away (archive_store_evict.py).
# A hold on the namespace itself, taken before the marker is read, is one that
# re-read sees, so either the evictor keeps the namespace or this process sees
# the marker. A hold no longer on its path means the namespace was renamed
# after the marker was read. mkdir makes the parts one at a time, so a rename
# between them leaves a namespace missing the parts made before it; the parts
# are checked once the hold shows which namespace this process holds. Any of
# these sends the claim round again, which makes the namespace afresh.
#
# `.claims` marks the namespace as one whose helpers claim it, the only kind an
# evictor retires: a helper revision from before claims makes its namespace and
# stages in it without a hold the evictor could see.
claim_namespace() {
    namespace=$store/$namespace_name
    mkdir -p "$namespace/objects" "$namespace/used" "$namespace/holds" 2>/dev/null &&
        touch "$namespace/.claims" "$namespace/holds/namespace@$identity" 2>/dev/null ||
        return
    # A test seam, as the publish gate is: the suite replaces the namespace
    # here, as a retirement between mkdir's parts would leave it. Nothing
    # that runs actions sets it.
    if [ -n "${ARCHIVE_MOUNT_TEST_CLAIM_GATE:-}" ]; then
        read -r _ <"$ARCHIVE_MOUNT_TEST_CLAIM_GATE"
    fi
    [ ! -e "$namespace/.retiring" ] &&
        [ -e "$namespace/holds/namespace@$identity" ] &&
        [ -d "$namespace/objects" ] && [ -d "$namespace/used" ]
}
claims=1
until claim_namespace; do
    # A retirement takes the evictor moments; a marker that outlasts these
    # tries was left by an evictor that died mid-retirement, and stays until
    # its next pass, so this action does without the shared store rather
    # than fail.
    if [ "$claims" -ge 3 ]; then
        log "cannot claim $namespace in the shared store; expanding into a private store"
        use_private_store
        claim_namespace || fail "cannot make $namespace"
        break
    fi
    claims=$((claims + 1))
    sleep 1
done
# Reads of one mount that found their entry unpublished under them; see expand.
# Each mount's expand runs in a command substitution of its own, a subshell, so
# every mount starts from this value and one mount's losses never count
# against another's.
unpublished_reads=0

# The store reads only the gzipped tar declared_archive's conversion writes,
# as the directory of its parts, which archive_parts.py names for its TAR_NAME.
require_tar() {
    [ -d "$1" ] && [ -f "$1/archive.tar.gz.part-aaa" ] ||
        fail "$1 is not the tar declared_archive writes; declare the archive through declared_archive, which converts and checks it"
}

# Refuse a tree holding a symlink whose target lies outside it, which would
# hand an action undeclared host or store content through its mount. Each
# link's target is walked from the link's own directory a component at a time
# with `cd -P`, which resolves `..` and nested links as the kernel would, and
# must never leave the tree; a component that is not a directory ends the walk,
# and what remains of the target may not climb.
confine_links() {
    root=$(cd -P "$1" && pwd -P)
    # The inline script is a script of its own, run by find with the links as
    # its arguments, so its expansions are meant for it, not for this shell.
    # shellcheck disable=SC2016
    find "$1" -type l -exec /bin/sh -c '
        root=$1
        shift
        for link; do
            target=$(readlink "$link")
            case $target in
            /*)
                printf "%s\n" "$link -> $target is absolute" >&2
                exit 1
                ;;
            esac
            (
                cd -P "${link%/*}" || exit 1
                rest=$target
                while [ -n "$rest" ]; do
                    part=${rest%%/*}
                    case $rest in
                    */*) rest=${rest#*/} ;;
                    *) rest= ;;
                    esac
                    if [ -z "$part" ] || [ "$part" = . ]; then
                        continue
                    fi
                    if [ ! -d "$part" ]; then
                        case /$rest/ in
                        */../*) exit 1 ;;
                        esac
                        break
                    fi
                    cd -P "$part" || exit 1
                    case $(pwd -P)/ in
                    "$root"/*) ;;
                    *) exit 1 ;;
                    esac
                done
            ) || {
                printf "%s\n" "$link -> $target leaves the expanded tree" >&2
                exit 1
            }
        done
    ' confine_links "$root" {} + ||
        fail "$2 holds a symlink that leaves its own tree"
}

# Expand ARCHIVE into the store under DIGEST unless an expansion is already
# published there; print the published object's path.
expand() {
    digest=$1
    archive=$2
    key=$digest
    entry=$namespace/$key
    if [ ! -L "$entry" ]; then
        # The parts in name order, which is the tar's: the digest is checked
        # over the parts tar reads below, joined in the same order, so a part
        # missing, or an order a locale's collation gave otherwise, fails here
        # rather than extracting.
        set -- "$archive"/archive.tar.gz.part-[a-z][a-z][a-z]
        actual=$(cat "$@" | shasum -a 256 | sed 's/ .*//')
        [ "$actual" = "$digest" ] ||
            fail "$archive has digest $actual, but the manifest declares $digest"
        # A staging name no concurrent expander shares, so each extracts into
        # a directory of its own. Until the staging tree is published, any
        # failure removes it, so a bad archive cannot fill the store with
        # partial trees one retry at a time.
        # The name carries its creator's identity, so an evictor can tell a
        # killed expansion's tree from one still being staged.
        staging=$(mktemp -d "$namespace/objects/$key.$identity.XXXXXX")
        # Publication-aware, since a signal deferred until `ln` returns can
        # run this after the link names the staging tree, which must then stay.
        trap '[ "$(readlink "$entry" 2>/dev/null)" = "objects/${staging##*/}" ] ||
            discard "$staging"' EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        started=$(date +%s)
        # An empty environment, so TAR_OPTIONS or TAR_READER_OPTIONS in the
        # action's cannot add options to these. One tar reads the joined parts
        # from a pipe, so no whole copy of a large tar is ever written; `-z`
        # since GNU tar detects compression only in a file. These are the
        # parts whose joined digest was checked above.
        cat "$@" | env -i PATH=/usr/bin:/bin tar -xzf - -C "$staging" --no-same-owner
        confine_links "$staging" "$archive"
        chmod -R a+rX,a-w "$staging"
        # A test seam: the suite holds every expander here until all of them
        # have staged, so the race below is exercised on every run. Nothing
        # that runs actions sets it.
        if [ -n "${ARCHIVE_MOUNT_TEST_PUBLISH_GATE:-}" ]; then
            read -r _ <"$ARCHIVE_MOUNT_TEST_PUBLISH_GATE"
        fi
        # symlink(2) refuses an existing name, so of several concurrent
        # expanders exactly one publishes; the rest discard their copies. The
        # published link is the completion marker: an expansion killed before
        # it leaves only an unpublished staging directory, which no reader
        # follows, and the trap above keeps whatever the link names.
        if ln -s "objects/${staging##*/}" "$entry" 2>/dev/null; then
            trap - EXIT
            log "expanded $archive into $entry in $(($(date +%s) - started))s"
        else
            [ -L "$entry" ] || fail "could not publish $entry"
            discard "$staging"
            trap - EXIT
            log "discarded a duplicate expansion of $archive: $entry was published first"
        fi
    fi
    # A hold on the published object, named for it and this process, keeps an
    # evictor from removing it while the action runs. An evictor must
    # unpublish an entry before it reads the holds on its object, so a hold
    # taken while the entry still named the object is one it sees; the second
    # readlink says which case this is. Nothing here removes a hold this
    # process took, even one the second readlink found useless: the name is
    # the same for every mount of the object, so an earlier mount of it may
    # rely on that very hold. An evictor drops it once the process has gone.
    object=$(readlink "$entry" 2>/dev/null) || object=
    if [ -n "$object" ]; then
        hold=$namespace/holds/${object#objects/}@$identity
        touch "$hold"
        # A test seam, as the publish gate is: the suite unpublishes the entry
        # here, as an evictor would. Nothing that runs actions sets it.
        if [ -n "${ARCHIVE_MOUNT_TEST_HOLD_GATE:-}" ]; then
            read -r _ <"$ARCHIVE_MOUNT_TEST_HOLD_GATE"
        fi
        if [ "$(readlink "$entry" 2>/dev/null)" = "$object" ]; then
            # Each read records use, so eviction can take the least recently
            # used.
            : >>"$namespace/used/$key"
            touch "$namespace/used/$key"
            printf '%s/%s\n' "$namespace" "$object"
            return
        fi
    fi
    # Evicted between the readlinks. An evictor takes the least recently read
    # first, and this read is about to expand and use the archive afresh, so
    # losing it again in a row is a fault, not a race.
    unpublished_reads=$((unpublished_reads + 1))
    [ "$unpublished_reads" -lt 3 ] ||
        fail "$entry was unpublished under every read of it"
    log "$entry was unpublished while it was read; reading it again"
    expand "$digest" "$archive"
}

# Read the manifest on a descriptor of its own, so the loop's commands keep the
# action's stdin.
# Earlier mounts, lowercased, one per line.
mounts=
while IFS='|' read -r mount digest archive strip <&3; do
    [ -n "$mount" ] || continue
    check_relative mount "$mount"
    [ "$strip" = "." ] || check_relative "$mount: strip prefix" "$strip"
    folded=$(printf '%s\n' "$mount" | tr '[:upper:]' '[:lower:]')
    while IFS= read -r earlier; do
        [ -n "$earlier" ] || continue
        case $folded/ in
        "$earlier"/*) fail "mount '$mount' sits inside or repeats an earlier mount" ;;
        esac
        case $earlier/ in
        "$folded"/*) fail "an earlier mount sits inside mount '$mount'" ;;
        esac
    done <<MOUNTS
$mounts
MOUNTS
    mounts="$mounts
$folded"
    # A symlink above the mount, left in a shared execution root, would carry
    # the mount somewhere else.
    case $mount in
    */*)
        above=
        rest=${mount%/*}/
        while [ -n "$rest" ]; do
            above=$above${rest%%/*}
            rest=${rest#*/}
            [ ! -L "$mount_root$above" ] ||
                fail "$above, above mount $mount, is a symlink"
            above=$above/
        done
        ;;
    esac
    digest=$(digest_value "$digest")
    require_tar "$archive"
    # A link left by an earlier run of this action in a shared execution
    # root is replaced; anything else at the mount path is a collision.
    [ -L "$mount_root$mount" ] || [ ! -e "$mount_root$mount" ] ||
        fail "$mount already exists in the input root"
    object=$(expand "$digest" "$archive")
    if [ "$strip" = "." ]; then
        target=$object
    else
        target=$object/$strip
    fi
    [ -d "$target" ] || fail "$mount: $archive has no directory $strip"
    case $mount in
    */*) mkdir -p "$mount_root${mount%/*}" ;;
    esac
    ln -sfn "$target" "$mount_root$mount"
done 3<"$manifest"

PATH=$caller_path
export PATH
if [ -n "$caller_has_cdpath" ]; then
    CDPATH=$caller_cdpath
    export CDPATH
fi
umask "$caller_umask"
if [ -n "$private_store" ]; then
    # The private store is removed on exit, so the command runs as a child.
    "$@" && status=0 || status=$?
    exit "$status"
fi
exec "$@"
