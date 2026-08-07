// Generated from protocol/companion.json.
// Manifest SHA-256: 1cdc7fce2dc132ec67cdd7d34c4fd724290d4ab637c10b742b37f13f5a6eacc2
// Do not edit by hand.
export const COMPANION_CAPABILITIES = Object.freeze({
  activeConnections: 1,
  retainedPeers: 5,
  resolvingListEntries: 5,
  persistedNotifyCharacteristics: 8,
  maxCccds: 40,
  overflowPolicy: 'least_recently_used',
});

export const BRIDGE_CHAR = Object.freeze({
  scheduleSync: 0,
  scheduleDigest: 1,
  currentTime: 2,
  newAlert: 3,
  battery: 4,
  eventRead: 5,
  prayerSettings: 6,
  beaconKey: 7,
  beaconControl: 8,
  multiAlarm: 9,
  dfuControl: 10,
  dfuPacket: 11,
  fsTransfer: 12,
  firmwareRevision: 13,
  weather: 14,
  steps: 15,
  stepsYesterday: 16,
  musicStatus: 17,
  musicArtist: 18,
  musicTrack: 19,
  musicAlbum: 20,
  musicPosition: 21,
  musicTotalLength: 22,
  musicTrackNumber: 23,
  musicTrackTotal: 24,
  musicPlaybackSpeed: 25,
  musicRepeat: 26,
  musicShuffle: 27,
  musicEvent: 28,
  callEvent: 29,
  tasksSync: 30,
  tasksDigest: 31,
  taskRead: 32,
  companionStatus: 33,
  companionVerify: 34,
  familyStateStatus: 35,
});

export const RECORDS = Object.freeze({
  "companion_management": {
    "eviction_policy_lru": 1,
    "protocol_version": 1,
    "status_size": 20
  },
  "family_state": {
    "att_errors": {
      "busy": 128,
      "protocol": 129,
      "storage": 130
    },
    "errors": {
      "busy": 1,
      "crc": 9,
      "invalid_state": 10,
      "none": 0,
      "queue_full": 2,
      "read": 5,
      "rename": 8,
      "spi": 4,
      "sync": 7,
      "timeout": 3,
      "unsupported": 11,
      "write": 6
    },
    "flags": {
      "storage_warning": 1
    },
    "operations": {
      "beacon_key": 6,
      "bond_store": 8,
      "boot_initialization": 11,
      "fs_transfer": 9,
      "multi_alarm": 4,
      "none": 0,
      "prayer_settings": 5,
      "resource_read": 10,
      "schedule": 1,
      "settings": 7,
      "task_streak": 3,
      "tasks": 2
    },
    "protocol_version": 1,
    "snapshot_schema_version": 1,
    "states": {
      "failed": 3,
      "idle": 0,
      "pending": 1,
      "succeeded": 2
    },
    "status_size": 16
  },
  "multi_alarm": {
    "capacity": 5,
    "protocol_version": 2,
    "record_size": 24
  },
  "prayer_settings": {
    "protocol_version": 2,
    "record_size": 9
  },
  "schedule": {
    "capacity": 16,
    "digest_size": 7,
    "protocol_version": 3,
    "record_size": 43,
    "record_version": 3
  },
  "task": {
    "capacity": 12,
    "digest_size": 9,
    "protocol_version": 2,
    "record_size": 31,
    "record_version": 2
  }
});
