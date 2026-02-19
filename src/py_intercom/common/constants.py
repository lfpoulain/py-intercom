SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SAMPLES = 480

OPUS_BITRATE = 64000
OPUS_COMPLEXITY = 5

JB_START_FRAMES = 6          # Jitter buffer pre-buffering (~60 ms at 10 ms/frame)
JB_MAX_FRAMES = 60           # Jitter buffer max size (~600 ms) — drops older packets beyond this
JB_SILENCE_GATE_FRAMES = 8   # Silence frames sent when JB empty before stopping (~80 ms)

CTRL_LIVENESS_TIMEOUT_S = 6.0   # TCP control: disconnect if no rx for this duration
CTRL_PING_INTERVAL_S = 0.05     # TCP control: ping interval

MAX_UDP_PAYLOAD_BYTES = 1200
AUDIO_UDP_PORT = 5000

CONTROL_PORT_OFFSET = 1
DISCOVERY_PORT_OFFSET = 2
DISCOVERY_BEACON_INTERVAL_S = 2.0
DISCOVERY_EXPIRY_S = 6.0

PACKET_HEADER_BYTES = 12
