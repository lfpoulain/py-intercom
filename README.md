# py-intercom
 
Intercom audio temps réel sur LAN (architecture client/serveur).
 
- Audio: UDP (Opus)
- Contrôle: TCP (JSON par ligne)
- UI: PySide6
- Return bus: capture audio locale côté serveur (ex: VB-Cable) mixée dans le flux des clients abonnés

## État actuel (résumé)

- Bus **fixes** : Régie (bus 0, bidirectionnel), Plateau (bus 1), VMix (bus 2).
- PTT côté client **par bus uniquement**.
- Modes PTT : `PTT` / `Toggle` / `Always On` sur le client Python et le client web.
- Raccourcis clavier globaux OS disponibles uniquement sur le client Python.
- Écoute : toggle séparé pour **Régie** et **Return bus**.
- Return bus configurable à chaud (activation + device d'entrée) pendant que le serveur tourne.
- Modèle de gains cohérent entre client desktop et serveur : talk gain partagé, gain master par bus, gain par sortie physique serveur, gain de retour par client, volume casque local côté client desktop.
- Outputs serveur configurables à chaud avec `device`, `bus`, `gain` et `VU` par sortie physique.
- Ports audio/contrôle **fixes** (5000/5001).

## Modèle de gains

| Niveau | Portée | Rôle |
|---|---|---|
| `input_gain_db` | Client desktop + serveur | **Talk gain** partagé par client. Si le client desktop l'applique localement, le serveur le synchronise ; sinon le serveur l'applique lui-même. |
| `AudioBus.gain_db` | Serveur | Gain master d'un bus (`Régie`, `Plateau`, `VMix`). |
| `OutputState.gain_db` | Serveur | Gain master d'une sortie physique serveur. |
| `return_gain_db` | Serveur, par client | Réglage d'écoute personnel du return bus reçu par ce client. |
| `output_gain_db` | Client desktop | Volume casque local du client Python. |

Dans la GUI serveur :

- slider **Talk** par client
- slider **Gain** par bus
- slider **Gain** + **VU** par output physique

## Prérequis

- Python 3.x
- Windows: `bin/opus.dll` est fourni. Les scripts `run_server.py` / `run_client.py` gèrent explicitement son chargement, et `run_web.py` prépare le dossier `bin/` pour le chargement des DLL nécessaires.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Lancement rapide (scripts .bat)

Les scripts dans `lunch script/` créent automatiquement le virtualenv `.venv` et installent les dépendances si nécessaire.

| Script | Description |
|--------|-------------|
| `lunch script\lancer_serveur_gui.bat` | Serveur en mode GUI (sans console) |
| `lunch script\lancer_client_gui.bat` | Client en mode GUI (sans console) |
| `lunch script\lancer_serveur_debug.bat` | Serveur en mode GUI + console avec logs debug |
| `lunch script\lancer_client_debug.bat` | Client en mode GUI + console avec logs debug |
| `lunch script\lancer_web.bat` | Client Web (HTTPS, auto-détection IP LAN) |

## Lancement manuel

### Serveur

```powershell
.\.venv\Scripts\python run_server.py --gui
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_server.py --gui --debug
```

Mode CLI (sans GUI) supporté pour l'usage serveur headless (`run_server.py` sans `--gui`).

### Client

```powershell
.\.venv\Scripts\python run_client.py --gui
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_client.py --gui --debug
```

## Build des exécutables

Le script `exe scripts\build_exe.ps1` compile le client et le serveur en `.exe` via PyInstaller (mode "onefile").

Prérequis: avoir déjà un virtualenv `.venv` et les dépendances installées (voir section Installation).

```powershell
& '.\exe scripts\build_exe.ps1'
```

Avec nettoyage des dossiers `build/` et `dist/` avant compilation:

```powershell
& '.\exe scripts\build_exe.ps1' -Clean
```

Les exécutables sont générés dans `dist/`.

- `dist\client.exe`
- `dist\server.exe`

## CI GitHub / Releases

Un workflow GitHub Actions est fourni dans `.github/workflows/windows-release.yml`.

- à chaque `push`, il build les `.exe` Windows
- il crée un tag de build unique
- il publie une GitHub Release avec :
  - `client.exe`
  - `server.exe`
  - une archive `.zip` contenant les deux

Par défaut :

- les builds de `main` / `master` sortent en release normale
- les autres branches sortent en `pre-release`

Note: en mode "onefile", les DLL (dont `opus.dll`) sont extraites au lancement dans un dossier temporaire (`%TEMP%\_MEI...`).

Au double-clic, les exécutables démarrent l'interface (équivalent `--gui`).

En cas de crash au démarrage (mode windowed), un fichier est écrit ici:

- `~\py-intercom\client_crash.txt`
- `~\py-intercom\server_crash.txt`

### Client Web (Plateau)

Pour les personnes sur plateau sans le client Python (PC, tablette Android, iPad) :

```powershell
# Défaut : HTTPS adhoc, port 8443, IP LAN auto-détectée
.\.venv\Scripts\python run_web.py

# Personnalisé
.\.venv\Scripts\python run_web.py --host 0.0.0.0 --port 8000 --ssl-adhoc --debug
```

Ouvrir `https://<ip>:8443/` dans un navigateur.

Options : `--host`, `--port`, `--debug`, `--ssl-adhoc`, `--ssl-cert`, `--ssl-key`.

> **Android / iOS** : HTTPS est obligatoire pour l'accès micro. Le certificat auto-signé (`--ssl-adhoc`) déclenche un avertissement navigateur à accepter une fois (Avancé → Continuer). Les serveurs intercom sont détectés automatiquement via le dropdown "Détection auto".

## Ports

- Audio (UDP): `5000` (fixe)
- Contrôle (TCP): `5001` (fixe)
- Discovery (UDP broadcast): `5002` (fixe)
- Client Web (HTTPS): `8443` (par défaut)

## Presets

- Serveur: `~\py-intercom\server_preset.json`
  - `buses` : `gain_db`, `feed_to_regie`
  - `outputs` : `device`, `bus_id`, `gain_db`
  - `clients` : `input_gain_db` persisté par `client_uuid`
- Client: `~\py-intercom\client_preset.json`
  - gains locaux : `input_gain_db`, `output_gain_db`
  - écoute retour : `return_gain_db`, `listen_return_bus`, `listen_regie`

## Aide

Lister les devices audio:

```powershell
.\.venv\Scripts\python run_client.py --list-devices
.\.venv\Scripts\python run_server.py --list-devices
```

## Licence

Ce projet est distribué sous licence **Creative Commons Attribution - NonCommercial - NoDerivatives 4.0 International** (`CC BY-NC-ND 4.0`).

Voir le fichier `LICENSE` ou <https://creativecommons.org/licenses/by-nc-nd/4.0/>.

## Documentation

Voir `playbook.md` pour le détail du protocole, des presets, du modèle de gains, des VU mètres et du workflow.
