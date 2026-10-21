# py-intercom

Intercom audio temps réel sur réseau local pour les équipes de production live, sous Windows.

- **Serveur Régie** : reçoit les micros, route l'audio sur trois bus (Régie, Plateau, VMix), alimente les sorties physiques et renvoie à chaque opérateur son propre mix-minus.
- **Client** : capture le micro, gère le push-to-talk par bus et joue le retour au casque.

Audio Opus 48 kHz mono, trames de 10 ms, files bornées qui privilégient toujours le direct plutôt que l'accumulation de retard.

## Télécharger

Aucune installation : télécharger l'exécutable puis double-cliquer.

- **Serveur Régie** : [py-intercom-server.exe](https://github.com/lfpoulain/py-intercom/releases/latest/download/py-intercom-server.exe)
- **Client** : [py-intercom-client.exe](https://github.com/lfpoulain/py-intercom/releases/latest/download/py-intercom-client.exe)

Ces liens pointent toujours vers la dernière version. Toutes les versions sont dans les [releases](https://github.com/lfpoulain/py-intercom/releases). Les exécutables ne sont pas signés : si Windows SmartScreen s'affiche, choisir « Informations complémentaires » puis « Exécuter quand même ».

## Lancer depuis les sources

Prérequis : Windows et Python 3.11 ou plus récent.

Double-cliquer sur un lanceur du dossier [`launchers/`](launchers) : au premier lancement, il crée l'environnement Python et installe les dépendances. Les variantes `debug` ouvrent en plus une console de diagnostic.

Ou depuis un terminal :

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run_server.py
.\.venv\Scripts\python run_client.py
```

Options : `--debug` (console et logs détaillés) et `--minimized`.

## Documentation

| Document | Pour qui | Contenu |
|---|---|---|
| [Guide d'exploitation](docs/guide-exploitation.md) | Opérateurs | Mise en place, PTT, gains, Bluetooth, dépannage, fichiers |
| [Architecture](docs/architecture.md) | Développeurs | Organisation du code, threads, routage, latence |
| [Protocole](docs/protocole.md) | Développeurs | Paquets audio UDP V2 et contrôle TCP V2 |
| [Développement](docs/developpement.md) | Développeurs | Tests, lint, build Opus et exécutables, conventions |
| [Roadmap](docs/roadmap.md) | Tous | Décisions actées, travaux ouverts, qualification |

## Organisation du dépôt

```text
src/py_intercom/   code de l'application (common/, server/, client/)
tests/             tests automatisés, sans carte son
launchers/         lanceurs Windows en double-clic
scripts/           build de la DLL Opus et des exécutables
bin/               opus.dll (libopus 1.6.1) et son manifeste de build
docs/              documentation
run_server.py      point d'entrée du serveur
run_client.py      point d'entrée du client
```

## Licence

Distribué sous licence **CC BY-NC-ND 4.0** : voir [LICENSE](LICENSE). Les dépendances et la provenance de la DLL Opus sont décrites dans [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
