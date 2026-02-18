# Catch-up depuis: « Alors parfois je demarre et j'ai 1sec de latence et parfois non. Tu vois d'ou ca pourrait venir ? »

## 1) Objectif de départ
Stabiliser la latence/jitter (notamment le démarrage parfois à ~1s), puis fiabiliser le flux audio entre:
- client desktop ↔ serveur
- client web ↔ bridge ↔ serveur

---

## 2) Diagnostic et axes traités

### A. Chaîne audio temps-réel
- Vérification des buffers UDP, jitter buffers (start/max/trim), PLC, files de mix.
- Vérification des callbacks audio d’entrée/sortie côté client desktop.
- Vérification du mix serveur et de la diffusion UDP.

### B. Plan de contrôle (TCP)
- Vérification de la fraîcheur des états de contrôle (PTT/mute/routing).
- Gestion plus robuste des messages de contrôle (taille max de ligne/buffer).

### C. Web client
- Refonte UX demandée: suppression PTT global, passage en PTT par bus.
- Synchronisation dynamique serveur ↔ web des états bus/routage.
- Debug de l’envoi audio web quand TX bouge côté UI mais rien côté serveur.

---

## 3) Changements réalisés (par composant)

## 3.1 `src/py_intercom/server/server.py`

### Robustesse contrôle / mix
- Durci la logique de contrôle (buffer de ligne borné) pour éviter des états incohérents/stales.
- Ajusté la logique de gating PTT dans le mix quand le contrôle devient stale.

### Synchronisation des états bus
- Ajout (puis réajusté selon itérations) de la propagation des états client dans `config`:
  - `ptt_buses`
  - `mute_buses`
  - `listen_return_bus`
- But: garder web/desktop alignés quand le serveur pousse des updates.

### Audio mix
- Ajustements sur trim en temps réel et comportement PLC selon les tests de jitter.
- (Tu as ensuite repris une partie de ces réglages pour revenir à ton tuning.)

---

## 3.2 `src/py_intercom/client/client.py`

### Résilience audio desktop
- Itérations sur:
  - tailles de buffers UDP/JB,
  - trim runtime,
  - fallback PLC sur trous courts,
  - paramètres d’ouverture Input/OutputStream (blocksize/latency).
- Objectif: réduire les glitches et la variance de latence au démarrage.

### Contrôle
- Durcissement de la lecture contrôle (borne max de ligne) pour éviter les dérives mémoire/protocole.

> Note: plusieurs de ces paramètres ont été ensuite modifiés/repris par toi pour ton tuning final (ce qui est normal vu l’approche expérimentale).

---

## 3.3 `src/py_intercom/common/jitter_buffer.py`

- Itérations sur la politique de gestion des petits gaps:
  - mode « conceal systématique » (PLC agressif)
  - puis retour vers « PLC si profondeur suffisante, sinon attendre ».
- Objectif: compromis entre continuité audio et stabilité de latence.

---

## 3.4 Web backend + bridge

### `src/py_intercom/web/app.py`
- Ajout/normalisation des events Socket.IO:
  - `ptt_bus`
  - `subscribe_bus`
  - `listen_return_bus`
  - `audio_in` (forward vers bridge)
- Gestion de session web propre (join/leave/disconnect).

### `src/py_intercom/web/bridge.py`
- Ajout support état bus complet:
  - `set_ptt_bus`, `set_mute_bus`, `set_listen_return_bus`
- Application des updates serveur reçues (`ptt_buses`, `mute_buses`, `listen_return_bus`, `buses`).
- **Fix TX web important (dernier patch):**
  - `handle_audio_in_int16` ne bloque plus l’envoi audio sur `_can_transmit_audio()` (état PTT bridge potentiellement en retard).
  - Le blocage local conserve uniquement `muted`.
  - Le gating PTT est laissé au frontend (source d’intention utilisateur).

---

## 3.5 Web frontend

### `src/py_intercom/web/templates/index.html`
- Suppression du PTT global.
- Mode verrouillé sur « PTT par bus ».
- Ajout/clarification panneau bus + return.

### `src/py_intercom/web/static/client.js`
- Refonte logique bus dynamique:
  - rendu bus depuis config serveur,
  - PTT par bus (press/release),
  - abonnement écoute par bus,
  - écoute return bus,
  - persistance localStorage.
- Synchronisation dynamique serveur ↔ web:
  - `routes` exploitées en live,
  - désactivation PTT sur bus non routé,
  - relâchement auto PTT si route devient inactive.
- Changement Socket.IO côté client web vers `io()` (négociation transport par défaut) pour éviter les soucis de connexion websocket forcée.

### `src/py_intercom/web/static/style.css`
- Styles du panneau bus.
- Badge visuel `Routé / Non routé`.

---

## 3.6 GUI client/serveur + persistance

### `src/py_intercom/client/gui.py`
- Ajustements de sync widgets bus/routing côté GUI.
- Persistance de champs de connexion (IP/port/name) même sans reconnect immédiat.

### `src/py_intercom/server/gui.py`
- Vérifications/ajustements de la cohérence de présentation états clients/routage (pendant les diagnostics).

---

## 3.7 Docker / déploiement web

Création de l’environnement docker web demandé:
- `docker/docker-compose.yml`
- `docker/web/Dockerfile`
- `docker/web/requirements.txt`
- `docker/web/entrypoint.sh`
- `docker/README.md`

Objectif: pouvoir build/run le web depuis ton repo (Gitea local) avec un flux reproductible.

---

## 4) Vérifications exécutées

- Plusieurs compilations ciblées Python (`compileall`) sur:
  - `src/py_intercom/web`
  - `src/py_intercom/server`
  - fichiers patchés (`bridge.py`, etc.)
- Vérifications de cohérence de présence/suppression de symboles JS/HTML après refonte PTT.

---

## 5) État actuel (résumé)

Ce qui est traité:
- UX web per-bus (sans PTT global).
- Sync dynamique bus/routage serveur ↔ web (avec indicateur routage).
- Chaîne control plus robuste.
- Correctif récent pour éviter drop TX web par gating bridge stale.

Ce que tu observais encore:
- « audio web pas envoyé au serveur » malgré UI TX.
- Le dernier correctif bridge vise précisément ce point (suppression du hard-gate `_can_transmit_audio` dans `handle_audio_in_int16`).

---

## 6) Remarque importante

Tu as fait ensuite des retouches de tuning (buffers, trim, latence low, peak limit, etc.) sur client/serveur. Donc ce catch-up liste:
1) les changements que j’ai effectivement apportés,
2) et le fait qu’une partie a pu être ensuite ajustée/revert de ton côté selon les essais temps réel.
