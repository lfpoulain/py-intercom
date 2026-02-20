# Playbook — py-intercom

> **Dernière mise à jour** : 19 février 2026 (rev 3)
>
> Ce document décrit l'état **réel** de l'application telle qu'elle est implémentée.
> Ce qui n'est pas implémenté est listé dans la section **Roadmap**.

---

## 1) Vue d'ensemble

`py-intercom` est un intercom audio temps réel **LAN** en architecture **client/serveur**.

| Composant | Rôle |
|---|---|
| **Serveur** | Reçoit les flux audio des clients, route par bus, fabrique un mix-minus par client (mix global − sa propre contribution), renvoie en UDP. Peut capturer un **return bus** (entrée audio locale, ex: VB-Cable) et le mixer dans le flux des clients abonnés. |
| **Client Python** | Capture micro → Opus → UDP serveur. Reçoit mix-minus → décode → casque. PTT par bus, raccourcis globaux OS, écoute Régie / Return bus. |
| **Client Web** | Client léger WebAudio + Socket.IO pour plateau (PC, tablette, mobile). Passe par un bridge Flask côté serveur web. |

Trois points d'entrée :

```
run_server.py   → serveur (GUI ou CLI)
run_client.py   → client Python (GUI ou CLI)
run_web.py      → client web (Flask + HTTPS)
```

## 2) Périmètre actuel

- Un serveur unique sur le LAN.
- Clients Python (GUI PySide6) et/ou clients web (navigateur).
- **3 bus fixes** : Régie (0), Plateau (1), VMix (2).
- **PTT par bus** avec raccourcis clavier globaux OS (`pynput`).
- **Écoute** : toggles séparés Régie / Return bus côté client.
- **Return bus** : capture audio locale côté serveur, mixée pour les clients abonnés.
- Gains serveur simplifiés (0 dB / unity).
- Presets JSON (écriture atomique) pour persister config serveur et client.
- Canal de contrôle TCP (keepalive, push config, kick).
- Auto-discovery LAN (UDP broadcast).

### Flux end-to-end

1. **Client** capture micro via `sounddevice` (callback input).
2. Applique le gain d'entrée, encode en **Opus**.
3. Envoie des paquets **UDP** au serveur (port `AUDIO_UDP_PORT`).
4. **Serveur** reçoit, décode Opus, met à jour l'état du client (VU, dernière activité).
5. Mixe périodiquement : mix global + **mix-minus** par client.
6. Si **return bus** activé : capture entrée audio locale → resample 48 kHz → mixe pour les clients ayant `listen_return_bus = true`.
7. Envoie le mix-minus (+ return bus si abonné) en **UDP** à chaque client.
8. **Client** décode Opus, bufferise (jitter buffer), joue dans le casque.
9. En parallèle, le canal **TCP control** synchronise PTT, flags d'écoute, états.

## 3) Concepts

### Identité client

- Chaque client possède un `client_uuid` persistant (stocké dans le preset).
- Le `client_id` (uint32) est dérivé : `crc32(client_uuid) & 0xFFFFFFFF`.
- Fonction factorisée : `common/identity.py` → `client_id_from_uuid()`.

### Bus

3 bus fixes :

| ID | Nom | Rôle |
|---|---|---|
| 0 | **Régie** | Bidirectionnel |
| 1 | **Plateau** | Diffusion |
| 2 | **VMix** | Diffusion |

Chaque bus maintient : `name`, `feed_to_regie`, liste de `source_uuids` (preset serveur).

### Return bus

Le serveur capture une entrée audio locale (ex: sortie programme VMix via VB-Cable) et la mixe dans le flux des clients abonnés.

- **Serveur** : activation via checkbox "Return" (ou `--return-enabled`), sélection device d'entrée, gain fixe 0 dB, VU mètre.
- **Client** : checkbox "Listen return bus" (persistée), VU "Return bus" alimenté via TCP control.

### Outputs

Un output = un `device` (index sounddevice) + un `bus_id`. Configurables depuis l'UI serveur.

### Attributs gérés

| Côté | Attributs |
|---|---|
| **Serveur** | Mute serveur, routage bus, état TCP (connecté / âge) |
| **Client** | PTT par bus, raccourcis globaux OS, `listen_regie`, `listen_return_bus`, gains (micro/casque/return) |

## 4) Protocole réseau

### Ports

| Service | Transport | Port par défaut |
|---|---|---|
| Audio | UDP | `5000` (`AUDIO_UDP_PORT`) |
| Contrôle | TCP | `5001` (`AUDIO_UDP_PORT + 1`) |
| Discovery | UDP broadcast | `5002` (`AUDIO_UDP_PORT + 2`) |
| Client web | HTTPS | `8443` |

### Audio (UDP)

- Header fixe de 12 bytes : `client_id` (u32) + `timestamp_ms` (u32) + `sequence_number` (u32)
- Payload : trame Opus encodée.
- Le serveur renvoie le mix-minus via `sendto()` vers l'adresse/port source observés.

### Contrôle (TCP)

Format : JSON par ligne (`\n`-delimited).

| Message | Direction | Contenu |
|---|---|---|
| `hello` | client → serveur | `client_id`, `client_uuid`, `name`, `udp_port` |
| `welcome` | serveur → client | Config initiale (buses) |
| `update` | serveur → client | Push config (buses + `return_vu_dbfs`) |
| `state` | client → serveur | `ptt_buses`, `listen_return_bus`, `listen_regie`, `return_gain_db` |
| `ping` / `pong` | bidirectionnel | Keepalive (`pong` inclut `return_vu_dbfs`) |
| `kick` | serveur → client | Déconnexion forcée |

Le champ `udp_port` dans `hello` permet au serveur de connaître le port UDP du client dès la connexion TCP, sans attendre le premier paquet audio.

### Auto-discovery (UDP broadcast)

- Beacon JSON envoyé toutes les 2s (`DISCOVERY_BEACON_INTERVAL_S`).
- Expiration après 6s sans beacon (`DISCOVERY_EXPIRY_S`).
- Module : `common/discovery.py` (`DiscoveryBeacon`, `DiscoveryListener`, `DiscoveredServer`).

```json
{
  "type": "py-intercom-beacon",
  "server_name": "<nom configurable>",
  "audio_port": 5000,
  "control_port": 5001,
  "version": 1
}
```

## 5) Audio / codec

| Paramètre | Valeur |
|---|---|
| Codec | Opus (`opuslib`) |
| Sample rate | 48 000 Hz |
| Channels | 1 (mono) |
| Frame | 480 samples = 10 ms |
| Bitrate | 64 kbps |
| Complexité | 5 |
| Format interne | `numpy.float32` dans `[-1.0, 1.0]` |

Le resampling serveur (si output != 48 kHz) est volontairement simple (robustesse > qualité audiophile).

### Jitter buffer

- Classe : `OpusPacketJitterBuffer` (`common/jitter_buffer.py`)
- Bufferise les **payloads Opus bruts** (pas les PCM décodés) → PLC Opus natif.
- `start_frames` = 3 (30 ms de buffering initial).
- `max_frames` = 60.
- Thread-safe (lock interne).
- **Fast-forward** : quand `expected_seq` est loin derrière le buffer, saute directement au frame le plus proche.
- **PLC** : pour les petits gaps (1-3 frames), retourne `b""` → Opus Packet Loss Concealment.
- Le décodage Opus se fait dans le callback audio (rythme fixe).

### Encodeur par client (serveur)

Chaque `ClientState` possède son propre `OpusEncoder` pour le mix-minus (évite la corruption d'état Opus entre clients).

### Utilitaires audio factorisés (`common/audio.py`)

- `db_to_linear()`, `apply_gain_db()`, `rms_dbfs()`
- `float32_to_int16_bytes()`, `int16_bytes_to_float32()`
- `limit_peak()` — écrêtage doux si peak > 1.0

## 6) Presets (JSON)

Écriture atomique via `common/jsonio.py`.

### Preset serveur

- Chemin : `~/py-intercom/server_preset.json`
- Contenu : `outputs` (liste), `buses` (mapping), `clients` (mapping), `return_enabled`, `return_input_device`, `return_input_device_name`, `return_input_device_hostapi`

### Preset client

- Chemin : `~/py-intercom/client_preset.json`
- Contenu : `server_ip`, `client_uuid`, `name`, `input_device` / `output_device` (+ `_name` / `_hostapi`), gains, `ptt_bus_keys`, `listen_regie`, `listen_return_bus`, `start_minimized`

## 7) Stack technique

### Dépendances

| Lib | Usage |
|---|---|
| **PySide6** | GUI serveur + client |
| **sounddevice** | Audio I/O |
| **numpy** | DSP / buffers |
| **opuslib** | Codec Opus |
| **loguru** | Logging |
| **pynput** | Hotkeys globaux OS (client) |
| **Flask** + **flask-socketio** | Client web backend |
| **cryptography** | HTTPS adhoc (client web) |

Windows : `bin/opus.dll` est fourni et chargé automatiquement par les scripts `run_*.py`.

### Organisation du code

```
src/py_intercom/
├── common/
│   ├── constants.py        # Ports, sample rate, frame size, Opus params
│   ├── audio.py            # DSP : gain, RMS, conversions, limit_peak
│   ├── packets.py          # Pack/unpack header UDP (12 bytes)
│   ├── opus_codec.py       # OpusEncoder / OpusDecoder
│   ├── jitter_buffer.py    # OpusPacketJitterBuffer
│   ├── devices.py          # list_devices, resolve_device, format_devices
│   ├── identity.py         # client_id_from_uuid (crc32)
│   ├── discovery.py        # DiscoveryBeacon, DiscoveryListener
│   ├── gui_utils.py        # DeviceWorker (QThread), is_checked
│   ├── theme.py            # Palette Qt, QSS, VuMeter, StatusIndicator, cell_vu, centered_checkbox, patch_combo
│   ├── jsonio.py           # read_json_file, atomic_write_json
│   └── logging.py          # setup_logging (loguru)
├── server/
│   ├── server.py           # IntercomServer (UDP rx, mix, broadcast, TCP ctrl)
│   ├── gui.py              # ServerWindow (PySide6)
│   └── main.py             # CLI entry point
├── client/
│   ├── client.py           # IntercomClient (capture, encode, rx, playback, TCP)
│   ├── gui.py              # ClientWindow (PySide6, hotkeys, discovery)
│   └── main.py             # CLI entry point
└── web/
    ├── bridge.py           # IntercomBridge (headless UDP/TCP client)
    ├── app.py              # Flask + Socket.IO (sessions, relay audio/ctrl)
    ├── main.py             # CLI entry point (run_web.py)
    ├── templates/index.html
    └── static/
        ├── client.js       # WebAudio capture/playback, Socket.IO, UI
        └── style.css
```

### Modèle d'exécution (threads)

**Serveur** :
- Thread RX UDP (ingestion payloads Opus dans JB par client)
- Thread mix (tick 10 ms : pop JB → décode → mix-minus → queue)
- Thread broadcast (lit queue → encode per-client → UDP sendto, mixe return bus si abonné)
- Thread accept TCP control + handlers par client
- N streams sounddevice de sortie (1 par output) avec callbacks
- (Optionnel) stream sounddevice d'entrée pour le return bus (callback → queue frames)

**Client Python** :
- Callback input (capture → gain → encode Opus → UDP)
- Thread RX UDP (push payload Opus brut dans `OpusPacketJitterBuffer`)
- Callback output (pop JB → décode Opus / PLC → gain → casque)
- Thread TCP control (keepalive + config)

**Client Web** :
- Thread bridge RX UDP + playout (tick 10 ms)
- Thread bridge TCP control
- Socket.IO relay (Flask, mode threading)

## 8) Configuration (.env)

Un fichier `.env` à la racine centralise les variables ajustables comme référence. Il n'est **pas lu automatiquement** par l'app (les valeurs sont dans `common/constants.py` et les arguments CLI).

Variables principales :

| Variable | Défaut | Description |
|---|---|---|
| `AUDIO_UDP_PORT` | `5000` | Port audio UDP |
| `SAMPLE_RATE` | `48000` | Fréquence d'échantillonnage |
| `FRAME_SAMPLES` | `480` | Taille de frame (10 ms) |
| `OPUS_BITRATE` | `64000` | Bitrate Opus |
| `OPUS_COMPLEXITY` | `5` | Complexité Opus |
| `DISCOVERY_BEACON_INTERVAL_S` | `2.0` | Intervalle beacon discovery |
| `DISCOVERY_EXPIRY_S` | `6.0` | Expiration serveur discovery |
| `WEB_PORT` | `8443` | Port du client web |
| `DEBUG` | `false` | Mode debug |

## 9) Lancement

### Scripts .bat (Windows)

| Script | Description |
|---|---|
| `lunch script\lancer_serveur_gui.bat` | Serveur GUI (sans console) |
| `lunch script\lancer_client_gui.bat` | Client GUI (sans console) |
| `lunch script\lancer_serveur_debug.bat` | Serveur GUI + console debug |
| `lunch script\lancer_client_debug.bat` | Client GUI + console debug |
| `lunch script\lancer_web.bat` | Client web (HTTPS adhoc) |

### Lancement manuel

```powershell
# Serveur
.\.venv\Scripts\python run_server.py --gui [--debug] [--minimized]

# Client
.\.venv\Scripts\python run_client.py --gui [--debug] [--minimized]

# Client web (défaut : HTTPS adhoc, port 8443, IP LAN auto)
.\.venv\Scripts\python run_web.py
.\.venv\Scripts\python run_web.py --host 0.0.0.0 --port 8000 --ssl-adhoc --debug
```

### Arguments CLI

**Serveur** : `--bind-ip`, `--output-device`, `--return-enabled`, `--return-input-device`, `--list-devices`, `--all-devices`, `--gui`, `--minimized`, `--debug`

**Client** : `--server-ip`, `--client-id`, `--client-uuid`, `--name`, `--input-device`, `--output-device`, `--input-gain-db`, `--output-gain-db`, `--list-devices`, `--all-devices`, `--gui`, `--minimized`, `--debug`

**Web** : `--host`, `--port`, `--debug`, `--ssl-adhoc`, `--ssl-cert`, `--ssl-key`

### Build exécutables

```powershell
& '.\exe scripts\build_exe.ps1'        # Build
& '.\exe scripts\build_exe.ps1' -Clean  # Build avec nettoyage
```

Produit `dist/client.exe` et `dist/server.exe` (PyInstaller onefile). Au double-clic → mode GUI.

Crash logs : `~/py-intercom/client_crash.txt` / `~/py-intercom/server_crash.txt`.

## 10) Workflow recommandé

1. Lancer le serveur (GUI) sur la machine régie.
2. Configurer les **outputs** (device + bus) dans la section Outputs.
3. Lancer un client (GUI), renseigner l'IP du serveur (ou auto-discovery), sélectionner devices, connecter.
4. Sur l'UI serveur :
   - Table clients : indicateur de statut (pastille verte/rouge), clients déconnectés grisés.
   - Ajuster les routes (colonnes Régie/Plateau/VMix).
   - Régler "Renvoyer dans Régie" pour Plateau/VMix si besoin.
5. Bouton **i** (client/serveur) pour diagnostiquer (ports, stats, buffers, underflows, control age).

Conseil : garder un device de sortie "VMix" séparé (VB-Cable) en output serveur si besoin d'intégration VMix.

## 11) Latence estimée (LAN)

### Client Python

| Composant | Valeur |
|---|---|
| Frame Opus | 10 ms |
| Jitter buffer (3 x 10 ms) | 30 ms |
| Driver WASAPI shared | ~10-20 ms |
| Réseau LAN | ~1-2 ms |
| Codec Opus (encode+decode) | < 3 ms |
| **Total estimé** | **~50-60 ms** |

### Client Web (surcoût vs Python)

| Composant | Valeur estimée |
|---|---|
| WebAudio ScriptProcessor buffer | ~42 ms (2048 samples @ 48 kHz) |
| Socket.IO WebSocket round-trip | ~1-5 ms (LAN) |
| Bridge jitter buffer | 30 ms (3 x 10 ms) |
| **Surcoût total estimé** | **~75-80 ms** |

## 12) VU mètres

| Emplacement | Mètre | Source |
|---|---|---|
| Serveur — table clients | VU par client | RMS frames décodées |
| Serveur — panneau | Return VU | RMS frames return bus |
| Client — panneau | Input VU | RMS micro capturé |
| Client — panneau | Output VU | RMS mix casque |
| Client — panneau | Return bus VU | Via TCP control (`pong` / `update`) |
| Client web | TX / RX bars | RMS calculé côté JS |

## 13) Client web (plateau)

Client léger **WebRTC + Socket.IO** pour les personnes sur plateau sans client Python. Fonctionne sur PC, tablette Android et iPad.

### Architecture (WebRTC Gateway)

```
Navigateur  <-- WebRTC (UDP audio Opus) & DataChannel -->  Flask/aiortc (bridge)  <-UDP/TCP->  IntercomServer
```

- **`app.py`** : Lance le serveur Flask + Socket.IO et démarre une boucle `asyncio` dédiée pour gérer `aiortc`.
- **`IntercomBridge`** (`web/bridge.py`) : Passerelle WebRTC ↔ UDP. Reçoit les trames audio Opus depuis WebRTC (via `aiortc`), ajoute l'en-tête UDP personnalisé (12 bytes), et transfère au serveur. Reçoit l'audio mixé du serveur et le pousse dans WebRTC.
- **Frontend** (`client.js`) : Capture micro et playback via `RTCPeerConnection`. Les messages de contrôle passent prioritairement par un `RTCDataChannel`, avec `Socket.IO` utilisé pour la signalisation (SDP) et comme fallback. L'annulation d'écho (AEC), la suppression de bruit et l'AGC sont gérés nativement par le navigateur.

### HTTPS (obligatoire pour mobile)

Les navigateurs bloquent l'accès au micro (`getUserMedia`) en HTTP non sécurisé.

- `--ssl-adhoc` : certificat auto-signé (nécessite `cryptography`). Avertissement navigateur à accepter une fois.
- `--ssl-cert` + `--ssl-key` : certificat fourni (ex: `mkcert`).

Par défaut, `run_web.py` lance en HTTPS adhoc sur le port 8443 avec détection automatique de l'IP LAN.

### Fonctionnalités & UX Plateau

- **PTT** : 3 boutons (Régie/Plateau/VMix). **Retour haptique** activé sur les appuis (via `navigator.vibrate`) pour confirmer l'ouverture du micro sans regarder l'écran.
- **Modes PTT par bus** : `PTT` (maintien), `Toggle` (appui pour basculer), `Always On` (toujours actif). Sélecteur en groupe de boutons segmentés.
- **Wake Lock API** : Maintien forcé de l'écran allumé (`navigator.wakeLock`) pendant la connexion, indispensable sur plateau pour éviter la mise en veille de la tablette.
- **Bannière "Audio Suspendu"** : Si l'OS mobile (particulièrement iOS) suspend le contexte audio en arrière-plan, une large bannière rouge cliquable apparaît pour le relancer.
- **Indicateur de Santé Réseau** : Une icône en haut à droite change de couleur (vert, orange, rouge) selon le RTT (Round Trip Time) WebRTC, permettant de diagnostiquer facilement les instabilités Wi-Fi sur le plateau.
- **Auto-discovery** : dropdown serveurs détectés + bouton rafraîchir + polling 3s.
- **Sélection périphériques** : micro et sortie modifiables sans rechargement de page. Le pipeline WebRTC est redémarré proprement à l'application.

### Pipeline audio (Zéro CPU sur le Bridge)

- **TX** : Micro → navigateur encodage Opus (avec AEC) → WebRTC UDP → `bridge.py` (extrait Opus brut + ajoute header) → UDP serveur.
- **RX** : Serveur → UDP mix-minus → `bridge.py` (`OpusPacketJitterBuffer` + extrait Opus brut) → WebRTC UDP → navigateur (décodage Opus) → `GainNode` (volume) → Haut-parleur.

Le bridge Python ne décode et ne ré-encode plus l'audio : il fait un transfert mémoire direct (Zéro CPU), permettant à un seul serveur Flask de supporter de nombreux clients.

### Anti-clipping / jitter buffer RX (Côté Serveur)

- **Silence gate** (`bridge.py`) : quand le jitter buffer est vide, le playout coroutine envoie des frames de silence pendant `JB_SILENCE_GATE_FRAMES` (~80 ms) avant de s'arrêter. Évite les trous dans le flux qui causaient des clics.
- L'essentiel de l'absorption de Jitter se fait nativement dans le navigateur grâce à WebRTC.

### Constantes injectées depuis le serveur

`FRAME_SAMPLES` et `SAMPLE_RATE` sont injectés dans le HTML via Jinja2 (`window.PY_INTERCOM_CONFIG`) depuis `common/constants.py` au moment du rendu de la page. Le JS lit ces valeurs avec fallback sur les défauts (480 / 48000). Modifier `constants.py` suffit — pas besoin de toucher le JS.

## 14) Dépannage rapide

**Client n'entend rien** :
- Vérifier la route (case bus) côté serveur
- Vérifier que l'output serveur pointe vers le bon bus
- Bouton i côté client : vérifier `control_connected`, `rx_packets`, `out_samplerate`

**Underflows côté serveur** (dans i) :
- Augmenter la latence/buffer au niveau driver/device (Windows)
- Essayer un autre host API/device

**Pas de contrôle TCP** :
- Vérifier que le port `5001` est joignable (pare-feu Windows)
- Lancer avec `--debug` pour logs réseau

**Son perdu après reconnexion** :
- Le client réutilise le même socket UDP (même port éphémère) pour éviter un blocage pare-feu
- Si persistant, vérifier les règles de pare-feu pour le port UDP

**Outputs muets après redémarrage serveur** :
- Le serveur retente l'ouverture des outputs et force un reopen après start (watchdog sur callbacks audio)
- En debug : chercher `output X retry` / `output X reopen failed` / erreurs PortAudio pour diagnostiquer
- Si besoin, relancer le serveur une fois pour laisser le retry s'exécuter

**Return bus ne fonctionne pas** :
- Vérifier "Return" coché côté serveur + device d'entrée sélectionné
- Vérifier "Listen return bus" coché côté client
- Vérifier le VU "Return VU" côté serveur

## 15) Limitations connues

- Resampling serveur volontairement simple (robustesse > qualité audiophile).
- Pas de chiffrement/authentification (usage LAN uniquement).
- WASAPI exclusive mode non supporté (causait echo/glitch).
- Client web : `ScriptProcessor` est déprécié mais maintenu dans tous les navigateurs. La migration vers AudioWorklet est reportée : le buffer 128 samples d'AudioWorklet cause des crackles massifs sur mobile (bug spec WebAudio #2632, avril 2025).
- Client web : pas de raccourcis clavier (supprimés volontairement — conflits avec les applications hôtes sur plateau).
- iOS Safari peut ignorer le hint `sampleRate: 48000` (retourne 44100 Hz) : le TX est rééchantillonné en JS (interpolation linéaire) avant envoi.

## 16) UI / Thème

- Thème sombre global via `QPalette` + QSS (`common/theme.py`).
- Boutons colorés par classe : `success` (vert), `danger` (rouge), `warning` (orange).
- `VuMeter` : widget horizontal compact vert/jaune/rouge, `setSizePolicy(Expanding, Fixed)`.
- `cell_vu(vu, h_margin)` : wrapper `QWidget` pleine largeur pour `setCellWidget` dans les tables.
- `centered_checkbox(cb)` : wrapper centré pour checkboxes en cellule.
- `patch_combo(combo)` : hauteur d'item compacte pour les `QComboBox`.
- Layouts serveur et client : colonnes (champs | boutons | options), marges cohérentes.
- Tables serveur : hauteur de ligne 32 px (alignée avec les combos), colonnes VU en `Stretch`.

## 17) Roadmap (non implémenté)

- AudioWorklet (remplacement de ScriptProcessor — attendre stabilisation mobile, bug WebAudio #2632)
- Jitter buffer adaptatif (ajustement dynamique de `start_frames`)
- AEC (annulation d'écho)
- Presets multiples (save-as / liste / load)
- EQ / compresseur / gate
- Contrôle externe (REST / OSC / MIDI)
- Multicast
- Lecture du `.env` par l'app au démarrage
