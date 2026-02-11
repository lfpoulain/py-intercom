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

## Lancer le serveur

```powershell
.\.venv\Scripts\python run_server.py --gui
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_server.py --gui --debug
```

## Lancer un client

```powershell
.\.venv\Scripts\python run_client.py --gui
```

Activer les logs debug:

```powershell
.\.venv\Scripts\python run_client.py --gui --debug
```

## Ports

- Audio (UDP): `5000` (par défaut)
- Contrôle (TCP): `5001` (par défaut)

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
