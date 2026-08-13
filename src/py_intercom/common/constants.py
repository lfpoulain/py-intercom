SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_SAMPLES = 480

MAX_GAIN_DB = 20.0

OPUS_BITRATE = 64000
OPUS_COMPLEXITY = 5
OPUS_DECODER_COMPLEXITY = 5  # Enables Deep PLC in a compatible libopus build.

# =============================================================================
# RÉGLAGES DE LATENCE ET BUFFERS (JITTER, SERVEUR)
# =============================================================================
# Ces paramètres permettent de trouver le bon équilibre entre une latence ultra-faible
# (type "Discord local") et la stabilité audio (éviter les clics ou micro-coupures).
# Si vous entendez des "clics" ou des hachures, AUGMENTEZ ces valeurs.
# Si vous ressentez trop de décalage (delay), DIMINUEZ ces valeurs.

# 1. Jitter Buffer (Réseau UDP : Client & Serveur)
# 1 frame = 10 ms
JB_START_FRAMES = 4          # Pré-buffering avant démarrage (~40 ms). 2 = très agressif (clics possibles), 6 = très sûr.
JB_MAX_FRAMES = 30           # Limite dure (~300 ms). Au-delà, on purge pour rattraper le direct.

# 2. Serveur (Files internes)
SERVER_MIX_QUEUE_MAX = 20          # File d'attente interne du mixage (~200 ms). Evite le lag de mixage.
SERVER_RETURN_FRAMES_MAX = 20      # File d'entrée de la carte son (Return bus, ~200 ms).
SERVER_OUT_MAX_BUFFER_S = 0.2      # Limite (en secondes) du buffer de sortie sur les haut-parleurs physiques (0.2s = 200 ms).

# =============================================================================

CTRL_LIVENESS_TIMEOUT_S = 6.0   # TCP control: disconnect if no rx for this duration
CTRL_PING_INTERVAL_S = 0.05      # TCP control: ping interval

MAX_UDP_PAYLOAD_BYTES = 1200
AUDIO_UDP_PORT = 5000

CONTROL_PORT_OFFSET = 1
DISCOVERY_PORT_OFFSET = 2
DISCOVERY_BEACON_INTERVAL_S = 2.0
DISCOVERY_EXPIRY_S = 6.0

# Audio transport V2. Packets are deliberately self-identifying and strict;
# the removed V1 layout is accepted nowhere in the runtime.
AUDIO_PACKET_V2_MAGIC = b"PYI2"
AUDIO_PACKET_V2_VERSION = 2
AUDIO_PACKET_V2_HEADER_BYTES = 32

AUDIO_PACKET_FLAG_CLIENT_TO_SERVER = 0x0001
AUDIO_PACKET_FLAG_SERVER_TO_CLIENT = 0x0002
AUDIO_PACKET_DIRECTION_FLAGS = (
    AUDIO_PACKET_FLAG_CLIENT_TO_SERVER | AUDIO_PACKET_FLAG_SERVER_TO_CLIENT
)
AUDIO_PACKET_V2_RESERVED_STREAM_ID = 0
