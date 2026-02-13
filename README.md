# py-intercom

Intercom audio temps réel sur LAN (architecture client/serveur).

- Audio: UDP (Opus)
- Contrôle: TCP (JSON par ligne)
- UI: PySide6

## Prérequis

- Python 3.x
- Windows: `bin/opus.dll` est fourni et automatiquement chargé par `run_server.py` / `run_client.py`.

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

Exemple en mode non-GUI avec bus retour (capture d'un input serveur type VB-Cable):

```powershell
.\.venv\Scripts\python run_server.py --bind-ip 0.0.0.0 --port 5000 --return-enabled --return-input-device 12 --return-gain-db -6
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_server.py --gui --debug
```

### Client

```powershell
.\.venv\Scripts\python run_client.py --gui
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_client.py --gui --debug
```

## Bus retour (serveur -> clients)

Le serveur peut capturer un flux audio depuis un périphérique d'entrée (par exemple `CABLE Output` de VB-Cable côté Windows Recording) et l'injecter dans le mix envoyé aux clients.

- Le bus retour est mixé dans le flux audio **Opus/UDP existant** (pas de second flux UDP séparé).
- Chaque client choisit s'il écoute ce bus via la case **Listen return bus**.

### UI associée

- **Serveur**: `Return` (enable), sélection du device d'entrée, `Return gain`, et vumètre `Return VU`.
- **Client**: toggle `Listen return bus` et vumètre `Return bus VU`.

### Comportement PTT après connexion

Le client n'a plus besoin d'appuyer une première fois sur PTT pour commencer à recevoir l'audio: le port UDP client est annoncé dès le `hello` TCP de contrôle.

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

- Audio (UDP): `5000` (par défaut)
- Contrôle (TCP): `5001` (par défaut)
- Discovery (UDP broadcast): `5002` (par défaut)
- Client Web (HTTPS): `8443` (par défaut)

## Presets

- Serveur: `~\py-intercom\server_preset.json`
- Client: `~\py-intercom\client_preset.json`

## Aide

Lister les devices audio:

```powershell
.\.venv\Scripts\python run_client.py --list-devices
.\.venv\Scripts\python run_server.py --list-devices
```

## Documentation

Voir `playbook.md`.
