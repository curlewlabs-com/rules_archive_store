# Independent consumer

A module of its own that uses the archive store rules only through their public
labels: a tree declared as its archive, read by a test, by an action of the
consumer's own rule, and, on macOS, as a linked tree under the consumer's Bun.

```sh
bazel test //...
```

It builds this checkout's module through `local_path_override`.
`tools/check_consumer.py --archive` runs it against a prepared release archive
instead.
