// Generated from protocol/companion.json.
// Manifest SHA-256: 6a3dc57bba4cb3ef146be88209256026e5a531f0fbad6079e34a14692dcd0949
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
});

export const RECORDS = Object.freeze({
  "companion_management": {
    "eviction_policy_lru": 1,
    "protocol_version": 1,
    "status_size": 20
  },
  "multi_alarm": {
    "capacity": 5,
    "record_size": 24
  },
  "prayer_settings": {
    "protocol_version": 1,
    "record_size": 9
  },
  "schedule": {
    "capacity": 64,
    "digest_size": 7,
    "protocol_version": 1,
    "record_size": 43,
    "record_version": 2
  },
  "task": {
    "capacity": 20,
    "digest_size": 9,
    "protocol_version": 1,
    "record_size": 31,
    "record_version": 1
  }
});
