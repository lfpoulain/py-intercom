# Playbook (V1) — py-intercom

Ce playbook décrit **l’état réel** de l’application telle qu’elle est implémentée aujourd’hui.

- Ce qui est **implémenté** est documenté dans les sections ci-dessous.
- Ce qui n’est **pas implémenté** est listé dans la section **Roadmap**.

## 1) Vue d’ensemble

`py-intercom` est un intercom audio temps réel **LAN** en architecture **client/serveur**.

- Le **serveur** reçoit les flux audio des clients, applique la config (routing/mute/gain), et renvoie à chaque client un mix “mix-minus” (le client reçoit le mix global moins sa propre contribution).
- Chaque **client** capture le micro, encode en Opus, envoie au serveur, puis lit le mix reçu dans son casque (option sidetone local).

L’app est utilisable via :

- `run_server.py --gui` (serveur + UI)
- `run_client.py --gui` (client + UI)

## 2) V1 — périmètre et comportement

Ce que couvre la V1 :

- Un serveur unique.
- Des clients Python (GUI) qui envoient leur micro et reçoivent un retour casque.
- Routage par bus (bus fixes), mute et gain par client, configuration d’outputs côté serveur.
- Presets JSON pour persister la config serveur et la config client.
- Canal de contrôle TCP (keepalive + push config + kick).

Ce que la V1 ne couvre pas : voir **Roadmap**.

### Flux end-to-end (résumé)

1. **Client** capture micro via `sounddevice` (callback input).
2. Le client applique le **gain d’entrée** et encode en **Opus**.
3. Le client envoie des paquets **UDP** au serveur (port audio).
4. Le serveur reçoit, décode Opus, et met à jour l’état du client (VU, dernière activité).
5. Le serveur mixe périodiquement et fabrique un mix “global” + calcule le **mix-minus** pour chaque client (mix global moins sa contribution).
6. Le serveur envoie le mix-minus en **UDP** à chaque client.
7. Le client décode Opus, bufferise et joue dans le casque (callback output). Optionnellement, le client ajoute un **sidetone** local.
8. En parallèle, le canal **TCP control** permet de synchroniser mute/gain/routes et d’afficher des états (connecté / âge).

## 3) Concepts actuels

### Sources (clients)

- Un client possède une identité **persistante** `client_uuid` (stockée dans le preset client).
- Le `client_id` (uint32) est dérivé de manière stable : `crc32(client_uuid) & 0xFFFFFFFF`.

Attributs gérés côté serveur :

- mute serveur
- gain (dB)
- routage vers bus
- état control TCP (connecté / âge)

### Bus

Dans l’implémentation actuelle, les bus sont **fixes** :

- `0` : `Regie`
- `1` : `Plateau`
- `2` : `VMix`

Chaque bus maintient :

- `default_all_sources` (si vrai : bus ouvert par défaut)
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

- `hello` (client -> serveur) : `client_id`, `client_uuid`, `name`, `mode`, `ptt_key`
- `welcome` (serveur -> client)
- `update` (serveur -> client) : push config (mute + routes)
- `state` (client -> serveur) : état client (ex: mute)
- `ping`/`pong` (keepalive)
- `kick` (serveur -> client)

## 5) Audio / codec

- Codec : **Opus** (`opuslib`)
- Format interne : `numpy.float32` dans `[-1.0, 1.0]`
- Taille de frame : `FRAME_SAMPLES` (typiquement 960 échantillons = 20ms à 48kHz)

Note importante :

- Le système est conçu autour d’un `SAMPLE_RATE` commun (48 kHz).
- Les streams de sortie serveur gèrent le cas où le device de sortie n’est pas à 48kHz via un resampling simple.

## 6) Presets (JSON)

Les presets sont **effectivement implémentés** en JSON, avec écriture atomique.

### Preset serveur

- Chemin par défaut : `~\py-intercom\server_preset.json`
- Contenu :
  - `outputs`: liste de `{device, bus_id}`
  - `buses`: mapping `bus_id -> {default_all_sources, source_uuids}`
  - `clients`: mapping `client_uuid -> {muted, gain_db, name}`

### Preset client

- Chemin par défaut : `~\py-intercom\client_preset.json`
- Contenu :
  - serveur (ip/port)
  - devices (input/output)
  - identité (`client_uuid`, name, mode, ptt_key)
  - gains, sidetone

## 7) Stack technique (dev)

### Langage / libs

- Python (projet `src/`)
- UI : **PySide6**
- Audio IO : **sounddevice**
- DSP / buffers : **numpy**
- Codec : **opuslib**
- Logging : **loguru**

Note : le `requirements.txt` contient aussi des dépendances liées à une piste “web” (Flask / SocketIO), mais **le client web n’est pas implémenté** dans la V1.

### Organisation du code

- `py_intercom/common/`
  - constantes (ports, sr, frame)
  - paquets (pack/unpack)
  - codec Opus
  - utilitaires (devices, json I/O, logging)

- `py_intercom/server/`
  - `IntercomServer` : réception UDP, mix, broadcast, contrôle TCP, presets
  - `gui.py` : UI serveur

- `py_intercom/client/`
  - `IntercomClient` : capture micro, envoi UDP, réception UDP, lecture casque, contrôle TCP
  - `gui.py` : UI client

### Modèle d’exécution (threads)

- Serveur :
  - thread RX UDP (décodage + ingestion)
  - thread mix + broadcast (mix global + mix-minus par client)
  - thread accept TCP control + handlers
  - N streams sounddevice de sortie (1 par output) avec callbacks

- Client :
  - callback input (capture -> encode -> UDP)
  - thread RX UDP (décodage -> buffer lecture)
  - callback output (lecture buffer -> casque)
  - thread TCP control (keepalive + config)

## 8) Workflow recommandé (opérationnel)

1. Lancer le serveur (GUI) sur la machine régie.
2. Configurer les **outputs** (device + bus) côté serveur.
3. Lancer un client (GUI), renseigner IP/port du serveur, sélectionner devices input/output, puis se connecter.
4. Sur l’UI serveur :
   - ajuster les routes (cases bus)
   - régler mute/gain par client
5. Utiliser le bouton `i` côté client/serveur pour diagnostiquer rapidement (ports, stats, buffers, underflows, control age).

Conseil : garder un device de sortie “VMix” séparé (VB-Cable) en output serveur si besoin d’intégration VMix.

## 9) Limitations connues (V1)

- Les bus sont **fixes** (pas de création/renommage dynamique via UI).
- Le resampling serveur (si output != 48kHz) est volontairement simple (objectif : robustesse avant qualité audiophile).
- Pas d’auto-discovery : l’IP serveur est saisie côté client.
- Pas de chiffrement/authentification : usage LAN.

## 10) Dépannage rapide

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

## 11) Roadmap (non implémenté)

Tout ce qui suit n’est **pas** implémenté dans la V1.

- Client web “plateau”
- Backend web (Flask / SocketIO) + WebAudio
- Auto-discovery LAN
- Jitter buffer adaptatif / réordonnancement
- AEC (annulation d’écho)
- Presets multiples (save-as / liste / load)
- Bus dynamiques (création / renommage via UI)
- PTT au niveau bus
- Enregistrement multi-bus (WAV/FLAC)
- EQ/comp/gate
- Contrôle externe (REST/OSC/MIDI)
- Multicast

