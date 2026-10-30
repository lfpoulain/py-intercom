"""Engine-wide constants. One audio frame is 480 samples, i.e. 10 ms."""

# --- Audio contract ---------------------------------------------------------
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SAMPLES = 480

MAX_GAIN_DB = 20.0

# --- Opus -------------------------------------------------------------------
OPUS_BITRATE = 64000
OPUS_COMPLEXITY = 5
OPUS_DECODER_COMPLEXITY = 5  # 5 is the threshold that enables Deep PLC.
# The intercom only carries voice: the VoIP mode codes it in hybrid (SILK +
# CELT) and each packet carries a low-rate copy of the previous frame. A lost
# frame is rebuilt from the next packet, already in the jitter buffer, with no
# added delay (same 6.5 ms lookahead). Measured on speech with 5 % isolated
# losses: lost frames come back at ~20 dB SNR, against ~4 dB with Deep PLC.
# This percentage sizes that redundancy, it is not a loss threshold.
OPUS_EXPECTED_LOSS_PERCENT = 10

# --- Latency budget ---------------------------------------------------------
# Every queue is bounded and drops its oldest frame when full: latency can
# never grow past these caps, at the cost of a short glitch.
JB_START_FRAMES = 4  # network pre-buffer before playout (~40 ms)
JB_MAX_FRAMES = 30  # jitter buffer hard cap (~300 ms)
SERVER_MIX_QUEUE_MAX = 20  # mixer -> broadcast queue (~200 ms)
SERVER_RETURN_FRAMES_MAX = 20  # Return bus input queue (~200 ms)
SERVER_OUT_MAX_BUFFER_S = 0.2  # queue in front of each physical output
# Latency cap: when a queue stays above its target for this many consecutive
# frames (0.5 s), one frame is dropped. Clock drift between sound cards can
# therefore never leave a lasting delay behind.
LATENCY_TRIM_AFTER_FRAMES = 50
# Jitter buffer target: what is left after each pop in steady state.
JB_TRIM_ABOVE_FRAMES = JB_START_FRAMES - 1
# A network burst or a stalled reader leaves more than JB_BURST_EXCESS_FRAMES
# above the target: once that lasts JB_BURST_TRIM_AFTER_FRAMES consecutive
# pops, the whole excess is dropped at once. The delay outlasts the server
# mixer's own catch-up (at most 100 ms), so a late reader is never starved.
JB_BURST_EXCESS_FRAMES = 2
JB_BURST_TRIM_AFTER_FRAMES = 10
# A jitter buffer underrun is concealed for at most this many frames; after a
# longer silence playout stops and resumes, without a new pre-buffer, from the
# first frame that was missing.
JB_MAX_CONCEALED_FRAMES = 2
# One frame of margin against the phase jitter between a producer thread and
# the sound card callback that reads the ring (server outputs and Return).
PCM_RING_TRIM_ABOVE_FRAMES = 1
# Client playout runs on the headphones' clock: each output callback asks the
# decoder for the next frame, which is queued this many frames ahead.
PLAYOUT_LEAD_FRAMES = 1
# The jitter buffer is read this long before the card needs the frame, not
# right after its callback: a frame arriving meanwhile (start of a talk spurt)
# is then played one period sooner. It leaves the decoder 4 ms to wake up and
# decode (~0.5 ms); see scripts/latency_bench.py.
PLAYOUT_DECODE_AHEAD_MS = 4.0

# --- Return ducking (optional, per client) ---------------------------------
RETURN_DUCK_DEPTH_DB = -15.0  # Return attenuation while voices are heard
RETURN_DUCK_THRESHOLD_DBFS = -45.0  # voice level that triggers ducking
RETURN_DUCK_ATTACK_DB_PER_FRAME = 5.0  # full depth in 30 ms
RETURN_DUCK_HOLD_FRAMES = 30  # keep ducked through 300 ms pauses between words
RETURN_DUCK_RELEASE_DB_PER_FRAME = 0.5  # back to full level in 300 ms

# --- Control plane (TCP) ----------------------------------------------------
CTRL_LIVENESS_TIMEOUT_S = 6.0  # session expires after this long without traffic
CTRL_PING_INTERVAL_S = 0.1  # the pong carries the Return VU: 10 Hz keeps it fluid
# Reconnection backoff of the client (±20 % jitter). A refused attempt is cheap
# on a LAN, so a restarted server is found again within a few seconds.
CTRL_RECONNECT_MIN_S = 1.0
CTRL_RECONNECT_MAX_S = 2.0

# --- Network ----------------------------------------------------------------
AUDIO_UDP_PORT = 5000
CONTROL_PORT_OFFSET = 1  # control TCP port = audio port + 1
DISCOVERY_PORT_OFFSET = 2  # discovery UDP broadcast port = audio port + 2
DISCOVERY_BEACON_INTERVAL_S = 2.0
DISCOVERY_EXPIRY_S = 6.0
MAX_UDP_PAYLOAD_BYTES = 1200

# --- Audio packet V2 --------------------------------------------------------
# Packets are self-identifying and strictly validated; V1 is accepted nowhere.
AUDIO_PACKET_V2_MAGIC = b"PYI2"
AUDIO_PACKET_V2_VERSION = 2
AUDIO_PACKET_V2_HEADER_BYTES = 32
AUDIO_PACKET_V2_RESERVED_STREAM_ID = 0

AUDIO_PACKET_FLAG_CLIENT_TO_SERVER = 0x0001
AUDIO_PACKET_FLAG_SERVER_TO_CLIENT = 0x0002
AUDIO_PACKET_DIRECTION_FLAGS = AUDIO_PACKET_FLAG_CLIENT_TO_SERVER | AUDIO_PACKET_FLAG_SERVER_TO_CLIENT
