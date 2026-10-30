// An excerpt of a bb_worker configuration: each runner names the archive
// store to its actions. Worker-side, so outside each action's key; an action's
// own variable of the same name wins (bb_worker.proto, environment_variables).
// The store sits beside the build directory, on its volume.
{
  buildDirectories: [{
    native: {
      buildDirectoryPath: '/var/lib/buildbarn-worker/build',
      cacheDirectoryPath: '/var/lib/buildbarn-worker/cache',
      maximumCacheFileCount: 1000000,
      maximumCacheSizeBytes: 64 * 1024 * 1024 * 1024,
      cacheReplacementPolicy: 'LEAST_RECENTLY_USED',
    },
    runners: [{
      endpoint: { address: 'unix:///var/lib/buildbarn-worker/runner.sock' },
      concurrency: 8,
      platform: {},
      workerId: {},
      environmentVariables: {
        RULES_ARCHIVE_STORE_DIR: '/var/lib/buildbarn-worker/archives',
      },
    }],
  }],
}
