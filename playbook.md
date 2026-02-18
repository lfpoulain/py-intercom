# Playbook (V1) — py-intercom

Ce playbook décrit **l’état réel** de l’application telle qu’elle est implémentée aujourd’hui.

- Ce qui est **implémenté** est documenté dans les sections ci-dessous.
- Ce qui n’est **pas implémenté** est listé dans la section **Roadmap**.

## 1) Vue d’ensemble

`py-intercom` est un intercom audio temps réel **LAN** en architecture **client/serveur**.

- Le **serveur** reçoit les flux audio des clients, applique la config (routing), et renvoie à chaque client un mix "mix-minus" (le client reçoit le mix global moins sa propre contribution). Le serveur peut aussi capturer un **return bus** (entrée audio locale, ex: VB-Cable) et le mixer dans le flux envoyé aux clients abonnés.
- Chaque **client** capture le micro, encode en Opus, envoie au serveur, puis lit le mix reçu dans son casque. Le client peut activer l'écoute Régie et/ou le return bus.

L’app est utilisable via :

- `run_server.py --gui` (serveur + UI)
- `run_client.py --gui` (client + UI)

## 2) V1 — périmètre et comportement

Ce que couvre la V1 :

- Un serveur unique.
- Des clients Python (GUI) qui envoient leur micro et reçoivent un retour casque.
- Bus **fixes** : Régie (bus 0), Plateau (bus 1), VMix (bus 2).
- **PTT** (Push-To-Talk) : uniquement **par bus** avec raccourcis clavier globaux OS par bus.
- Écoute : toggles séparés **Régie** / **Return bus** côté client.
- **Return bus** : le serveur capture une entrée audio locale (ex: sortie programme VMix via VB-Cable) et la mixe dans le flux des clients abonnés.
- UX serveur : suppression des contrôles de gain return et gain client (fixés à 0 dB / unity) et suppression de l'affichage du mode client.
- Presets JSON pour persister la config serveur et la config client.
- Canal de contrôle TCP (keepalive + push config + kick).

Ce que la V1 ne couvre pas : voir **Roadmap**.

### Flux end-to-end (résumé)

1. **Client** capture micro via `sounddevice` (callback input).
2. Le client applique le **gain d’entrée** et encode en **Opus**.
3. Le client envoie des paquets **UDP** au serveur (port audio).
4. Le serveur reçoit, décode Opus, et met à jour l’état du client (VU, dernière activité).
5. Le serveur mixe périodiquement et fabrique un mix "global" + calcule le **mix-minus** pour chaque client (mix global moins sa contribution).
5b. Si le **return bus** est activé, le serveur capture l'entrée audio locale, la resample à 48kHz, et la mixe dans le flux des clients ayant `listen_return_bus = true`.
6. Le serveur envoie le mix-minus (+ return bus si abonné) en **UDP** à chaque client.
7. Le client décode Opus, bufferise et joue dans le casque (callback output).
8. En parallèle, le canal **TCP control** permet de synchroniser les états PTT par bus et les flags d'écoute (Régie/Return bus) et d’afficher des états (connecté / âge).

## 3) Concepts actuels

### Sources (clients)

- Un client possède une identité **persistante** `client_uuid` (stockée dans le preset client).
- Le `client_id` (uint32) est dérivé de manière stable : `crc32(client_uuid) & 0xFFFFFFFF`.

Attributs gérés côté serveur :

- mute serveur
- routage vers bus
- état control TCP (connecté / âge)

Attributs gérés côté client :

- PTT par bus (active le micro sur un bus spécifique)
- raccourcis clavier globaux OS par bus via `pynput` (capturés même si l'app n'a pas le focus)
- `listen_regie` : écoute du bus Régie (persisté dans le preset client)
- `listen_return_bus` : abonnement au return bus (persisté dans le preset client)

### Return bus

Le serveur peut capturer une **entrée audio locale** (ex: sortie programme VMix routée via VB-Cable) et la mixer dans le flux envoyé aux clients abonnés.

Côté serveur :

- Activation via checkbox "Return" dans l'UI (ou `--return-enabled` en CLI)
- Sélection du device d'entrée (combo dans l'UI, ou `--return-input-device` en CLI)
- Gain fixe à 0 dB (unity)
- VU mètre "Return VU" dans le panneau serveur
- L'audio capturé est resampleé à 48kHz et découpé en frames de 480 samples
- Les frames sont mixées dans le mix-minus uniquement pour les clients ayant `listen_return_bus = true`

Côté client :

- Checkbox "Listen return bus" dans l'UI (persistée dans le preset)
- VU mètre "Return bus VU" dans le panneau Meters (alimenté par le serveur via le canal de contrôle TCP)
- Le flag `listen_return_bus` est envoyé au serveur dans les messages `state`

### Bus

Dans l’implémentation actuelle, les bus sont **fixes** :

- `0` : **Régie** (bus bidirectionnel)
- `1` : **Plateau** (diffusion)
- `2` : **VMix** (diffusion)

Chaque bus maintient :

- `name`, `feed_to_regie`
- une liste de `source_uuids` (persistée dans le preset serveur)

### Outputs

Un output correspond à :

- un `device` (index sounddevice)
- un `bus_id` (quel bus jouer sur cet output)

Les outputs sont configurables depuis l’UI serveur.

## 4) Protocole réseau

### Audio (UDP)

- Transport : **UDP**
- Port serveur : `AUDIO_UDP_PORT` (par défaut `5000`)
- Payload : trames Opus
- Header (12 bytes) :
  - `client_id` (uint32)
  - `timestamp_ms` (uint32)
  - `sequence_number` (uint32)

Le serveur renvoie à chaque client un flux UDP (mix-minus), via `sendto()` vers l’adresse/port source observés.

### Contrôle (TCP)

- Transport : **TCP**
- Port : `AUDIO_UDP_PORT + 1` (par défaut `5001`)
- Format : JSON par ligne (JSON + `\n`)

Types actuellement utilisés :

- `hello` (client -> serveur) : `client_id`, `client_uuid`, `name`, `udp_port`
- `welcome` (serveur -> client)
- `update` (serveur -> client) : push config (buses + `return_vu_dbfs`)
- `state` (client -> serveur) : `ptt_buses`, `listen_return_bus`, `listen_regie`
- `ping`/`pong` (keepalive, le `pong` inclut `return_vu_dbfs`)
- `kick` (serveur -> client)

Note : le champ `udp_port` dans `hello` permet au serveur de connaître le port UDP du client dès la connexion TCP, sans attendre le premier paquet UDP. Cela résout le problème où un client en mode PTT devait appuyer une première fois sur PTT avant de recevoir l'audio.

### Auto-discovery (UDP broadcast)

- Transport : **UDP broadcast** (`255.255.255.255`)
- Port : `AUDIO_UDP_PORT + 2` (par défaut `5002`)
- Intervalle : toutes les 2 secondes (`DISCOVERY_BEACON_INTERVAL_S`)
- Expiration : un serveur disparaît de la liste client après 6 secondes sans beacon (`DISCOVERY_EXPIRY_S`)

Le serveur envoie un beacon JSON :

```json
{
  "type": "py-intercom-beacon",
  "server_name": "<nom configurable>",
  "audio_port": 5000,
  "control_port": 5001,
  "version": 1
}
```

Côté client, un `DiscoveryListener` écoute sur le port discovery et maintient une liste de serveurs détectés. La GUI client affiche un combo "Discovered" qui remplit automatiquement l'IP et le port quand un serveur est sélectionné.

Côté serveur, le beacon est activable via la checkbox "Discovery" dans la GUI, et le nom du serveur est configurable via le champ "Name".

Module : `common/discovery.py` (`DiscoveryBeacon`, `DiscoveryListener`, `DiscoveredServer`).

## 5) Audio / codec

- Codec : **Opus** (`opuslib`)
- Format interne : `numpy.float32` dans `[-1.0, 1.0]`
- Taille de frame : `FRAME_SAMPLES` = **480 échantillons = 10ms** à 48kHz
- Bitrate Opus : 64 kbps, complexité 5

Note importante :

- Le système est conçu autour d'un `SAMPLE_RATE` commun (48 kHz).
- Les streams de sortie serveur gèrent le cas où le device de sortie n'est pas à 48kHz via un resampling simple.

### Jitter buffer (Opus payload)

- Classe : `OpusPacketJitterBuffer` (`common/jitter_buffer.py`)
- Bufferise les **payloads Opus bruts** (pas les PCM décodés) pour permettre le PLC Opus natif.
- `start_frames` = 3 (30ms de buffering initial avant de commencer la lecture).
- `max_frames` = 60.
- Thread-safe (lock interne) : le thread RX push, le callback audio pop.
- **Fast-forward** : quand `expected_seq` est loin derrière le buffer (gap > `start_frames`), saute directement au frame le plus proche au lieu de crawler +1 par pop.
- **PLC** : pour les petits gaps (1-3 frames manquants), retourne `b""` que le décodeur Opus interprète comme Packet Loss Concealment.
- Le décodage Opus se fait dans le callback audio (pas dans le thread RX), ce qui garantit un rythme de décodage fixe.

### Encodeur par client (serveur)

- Chaque `ClientState` possède son propre `OpusEncoder` pour le mix-minus.
- Évite la corruption d'état Opus quand plusieurs mix-minus sont encodés en parallèle (un encodeur partagé causait des artefacts métalliques).

## 6) Presets (JSON)

Les presets sont **effectivement implémentés** en JSON, avec écriture atomique.

### Preset serveur

- Chemin par défaut : `~\py-intercom\server_preset.json`
- Contenu :
  - `outputs`: liste de `{device, bus_id}`
  - `buses`: mapping `bus_id -> {name, feed_to_regie, source_uuids}`
  - `clients`: mapping `client_uuid -> {name}`
  - `return_enabled`, `return_input_device` (config return bus)

### Preset client

- Chemin par défaut : `~\py-intercom\client_preset.json`
- Contenu :
  - serveur (ip/port)
  - devices (input/output)
  - identité (`client_uuid`, name)
  - gains
  - `ptt_bus_keys` : mapping `bus_id -> raccourci` pour PTT par bus
  - `listen_regie` : écoute du bus Régie
  - `listen_return_bus` : abonnement au return bus

## 7) Stack technique (dev)

### Langage / libs

- Python (projet `src/`)
- UI : **PySide6** (thème natif projet via palette + QSS)
- Audio IO : **sounddevice**
- DSP / buffers : **numpy**
- Codec : **opuslib**
- Logging : **loguru**
- Hotkeys globaux : **pynput**

Note : le `requirements.txt` contient aussi des dépendances liées à une piste “web” (Flask / SocketIO), utilisés par le **client web**.

### Organisation du code

- `py_intercom/common/`
  - constantes (ports, sr, frame)
  - paquets (pack/unpack)
  - codec Opus
  - utilitaires (devices, json I/O, logging)
  - `discovery.py` : auto-discovery LAN (beacon + listener)
  - `theme.py` : application du thème natif Qt (palette + QSS) + widgets `VuMeter` (barre colorée vert/jaune/rouge) et `StatusIndicator` (pastille en ligne/hors ligne)

- `py_intercom/server/`
  - `IntercomServer` : réception UDP, mix, broadcast, contrôle TCP, presets
  - `gui.py` : UI serveur

- `py_intercom/client/`
  - `IntercomClient` : capture micro, envoi UDP, réception UDP, lecture casque, contrôle TCP
  - `gui.py` : UI client

- `py_intercom/web/`
  - `bridge.py` : `IntercomBridge` — client headless (UDP audio + TCP control) qui fait le pont entre le serveur intercom et le backend Flask
  - `app.py` : application Flask + Socket.IO — gère les sessions web, relaie audio et contrôle entre le navigateur et le bridge
  - `main.py` : point d'entrée du serveur web (`run_web.py`)
  - `templates/index.html` : page unique du client web
  - `static/client.js` : logique frontend (WebAudio capture/playback, Socket.IO, UI)
  - `static/style.css` : styles du client web

### Modèle d’exécution (threads)

- Serveur :
  - thread RX UDP (ingestion payloads Opus dans JB par client)
  - thread mix (tick 10ms : pop JB → décode → mix-minus → queue)
  - thread broadcast (lit queue → encode per-client → UDP sendto, mixe return bus si abonné)
  - thread accept TCP control + handlers
  - N streams sounddevice de sortie (1 par output) avec callbacks
  - (optionnel) stream sounddevice d'entrée pour le return bus (callback → resample → queue frames)

- Client :
  - callback input (capture → resample → encode Opus → UDP)
  - thread RX UDP (push payload Opus brut dans `OpusPacketJitterBuffer`)
  - callback output (pop JB → décode Opus / PLC → mix → casque)
  - thread TCP control (keepalive + config)

## 8) Workflow recommandé (opérationnel)

1. Lancer le serveur (GUI) sur la machine régie.
2. Configurer les **outputs** (device + bus) dans la section Outputs (Refresh devices, sélection device/bus, Add output).
3. Lancer un client (GUI), renseigner l’IP du serveur, sélectionner devices input/output, puis se connecter.
4. Sur l'UI serveur :
   - la table clients affiche un **indicateur de statut** (pastille verte = en ligne, rouge = hors ligne) ; les clients déconnectés sont grisés
   - ajuster les routes (colonnes Régie/Plateau/VMix)
   - régler "Renvoyer dans Régie" pour Plateau/VMix si besoin
5. Utiliser le bouton **ℹ** (en bas à droite) côté client/serveur pour diagnostiquer rapidement (ports, stats, buffers, underflows, control age, adresses clients).

Conseil : garder un device de sortie "VMix" séparé (VB-Cable) en output serveur si besoin d'intégration VMix.

## 9) Latence end-to-end estimée (LAN)

| Composant | Valeur |
|---|---|
| Frame Opus | 10 ms |
| Jitter buffer (3 × 10ms) | 30 ms |
| Driver WASAPI shared | ~10-20 ms |
| Réseau LAN | ~1-2 ms |
| Codec Opus (encode+decode) | < 3 ms |
| **Total estimé** | **~50-60 ms** |

## 10) VU mètres

### Côté serveur

- **VU par client** : affiché dans la table clients (colonne VU), calculé à partir du RMS des frames décodées.
- **Return VU** : affiché dans le panneau Server, calculé à partir du RMS des frames capturées par le return bus.

### Côté client

- **Input VU** : niveau micro capturé.
- **Output VU** : niveau du mix reçu joué dans le casque.
- **Return bus VU** : niveau du return bus, transmis par le serveur via les messages `pong` et `update` du canal de contrôle TCP.

## 11) Limitations connues (V1)

- Le resampling serveur (si output != 48kHz) est volontairement simple (objectif : robustesse avant qualité audiophile).
- Pas de chiffrement/authentification : usage LAN.
- WASAPI exclusive mode non supporté (retiré — causait echo/glitch, à revisiter).

## 12) Dépannage rapide

- Si un client n’entend rien :
  - vérifier la route (case bus) côté serveur
  - vérifier que l’output serveur pointe vers le bon bus
  - ouvrir `i` côté client pour vérifier `control_connected`, `rx_packets`, `out_samplerate`

- Si tu vois des `Underflows` côté serveur (dans `i`) :
  - augmenter la latence/buffer au niveau driver/device (Windows)
  - essayer un autre host API/device

- Si la connexion “semble” OK mais pas de contrôle :
  - vérifier que le TCP control est joignable sur `5001` (pare-feu Windows)
  - lancer avec `--debug` pour logs réseau

- Si le son ne passe plus après déconnexion/reconnexion du client :
  - le client réutilise le même socket UDP (même port éphémère) pour éviter un blocage par le pare-feu Windows
  - si le problème persiste, vérifier les règles de pare-feu pour le port UDP utilisé

- Si le return bus ne fonctionne pas :
  - vérifier que "Return" est coché côté serveur et qu'un device d'entrée est sélectionné
  - vérifier que "Listen return bus" est coché côté client
  - vérifier le VU "Return VU" côté serveur (doit bouger si l'audio arrive sur le device)

## 13) Client web (plateau)

Un client léger en **WebAudio + WebSocket** pour les personnes sur plateau qui n'ont pas le client Python. Fonctionne sur PC, tablette Android et iPad.

### Architecture

```bash
Navigateur  ←Socket.IO (wss)→  Flask/SocketIO (bridge)  ←UDP/TCP→  IntercomServer
```

- **`IntercomBridge`** (`web/bridge.py`) : client headless qui gère UDP audio (Opus encode/decode) et TCP control vers le serveur intercom.
- **`app.py`** : application Flask + Socket.IO qui relaie audio PCM (int16 LE) et messages de contrôle entre le navigateur et le bridge. Intègre un `DiscoveryListener` pour la détection automatique des serveurs.
- **Frontend** (`client.js`) : capture micro via WebAudio `ScriptProcessor`, playback via `ScriptProcessor` + `GainNode`, communication via Socket.IO.

### HTTPS (obligatoire pour mobile)

Les navigateurs mobiles (Android, iOS) **bloquent `getUserMedia`** (accès micro) sur les connexions HTTP non sécurisées. Le serveur web supporte HTTPS :

- **`--ssl-adhoc`** : certificat auto-signé généré à la volée (nécessite `cryptography`). Le navigateur affichera un avertissement à accepter une fois.
- **`--ssl-cert` + `--ssl-key`** : certificat fourni (ex: via `mkcert` pour un certificat local de confiance).

Par défaut, `run_web.py` (sans arguments) lance en **HTTPS adhoc** sur le port **8443** avec détection automatique de l'IP LAN.

### Auto-discovery

- **Backend** : un `DiscoveryListener` écoute les beacons UDP sur le port 5002.
- **REST** : endpoint `GET /api/discovery` retourne la liste des serveurs détectés en JSON.
- **Socket.IO** : événement `discovery` émis au connect avec la liste courante.
- **Frontend** : dropdown "Détection auto" peuplé par polling toutes les 3s. Sélectionner un serveur remplit automatiquement IP et port. Si le champ IP est vide, le premier serveur détecté est auto-sélectionné.

### Pipeline audio

- **TX** : micro → `ScriptProcessor` (capture à `sampleRate` du contexte) → resample linéaire vers 48kHz (avec tracking de phase) → découpe en frames de 480 samples → conversion float32 → int16 LE → Socket.IO `audio_in` → bridge encode Opus → UDP vers serveur.
- **RX** : serveur envoie mix-minus UDP → bridge `OpusPacketJitterBuffer` → playout thread (tick 10ms) → décode Opus → float32 → int16 LE → Socket.IO `audio_out` → frontend int16→float32 → resample vers contexte SR → `playQueue` → `ScriptProcessor` → `GainNode` → haut-parleur.

### Fonctionnalités

- **PTT** : 3 boutons (Régie/Plateau/VMix) + raccourcis `1/2/3` + `Espace` pour Régie
- **Écoute** : toggles **Régie** / **Return bus**
- **Volume** : slider 0–150% via `GainNode`
- **VU mètres** : TX (vert) et RX (bleu) avec peak decay
- **Indicateur de connexion** : dot vert/gris + label texte
- **Auto-discovery** : dropdown serveurs détectés + bouton rafraîchir
- **Persistance** : UUID client + settings (IP, nom, écoute, volume) en `localStorage`
- **Jitter buffer** : `OpusPacketJitterBuffer` côté bridge (identique au client Python)
- **Mobile** : `AudioContext.resume()` pour débloquer l'audio sur Android/iOS, détection de contexte non sécurisé avec message explicite, responsive CSS, boutons tactiles

### Interface

- Design dark avec tokens CSS (surfaces, bordures, accents)
- Layout card-based : carte Connexion, carte Audio, carte Journal
- Icônes SVG inline (pas de dépendance externe)
- Font Inter (Google Fonts)
- Media query mobile (`max-width: 560px`) : boutons plus grands, inputs `font-size: 16px` (évite le zoom auto iOS/Android)

### Lancement

```powershell
# Défaut : HTTPS adhoc, port 8443, IP LAN auto-détectée
.\.venv\Scripts\python run_web.py

# Personnalisé
.\.venv\Scripts\python run_web.py --host 0.0.0.0 --port 8000 --ssl-adhoc --debug
```

Options : `--host`, `--port`, `--debug`, `--ssl-adhoc`, `--ssl-cert`, `--ssl-key`.

Le client web est accessible à `https://<ip>:8443/` (HTTPS).

### Latence supplémentaire (vs client Python)

| Composant | Valeur estimée |
|---|---|
| WebAudio ScriptProcessor buffer | ~42ms (2048 samples @ 48kHz) |
| Socket.IO WebSocket round-trip | ~1-5ms (LAN) |
| Bridge jitter buffer | 30ms (3 × 10ms) |
| **Surcoût total estimé** | **~75-80ms** |

## 14) Roadmap (non implémenté)

Tout ce qui suit n'est **pas** implémenté.

- AudioWorklet (remplacement de ScriptProcessor pour le client web)
- Jitter buffer adaptatif (ajustement dynamique de `start_frames`)
- AEC (annulation d'écho)
- Presets multiples (save-as / liste / load)
- EQ/comp/gate
- Contrôle externe (REST/OSC/MIDI)
- Multicast

