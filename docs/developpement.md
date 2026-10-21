# Développement

## Environnement

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
```

| Fichier | Contenu |
|---|---|
| `requirements.txt` | Dépendances d'exécution, versions figées |
| `requirements-dev.txt` | pytest, ruff, PyInstaller |
| `pyproject.toml` | Métadonnées du paquet, configuration ruff et pytest |

## Tests et lint

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
```

Aucune carte son n'est nécessaire. Les tests GUI tournent hors écran. La suite couvre notamment :

- **intégration** (`test_integration.py`) : un vrai serveur et de vrais clients sur la boucle locale, avec Opus réel ; seule la carte son est simulée. Handshake, routage Régie/Plateau, mix-minus, relâchement du PTT, latence, déconnexion et reconnexion ;
- protocole V2, tokens périmés, usurpation d'adresse, paquets invalides ;
- routage et mix-minus, jitter buffer et PLC, rings, codec Opus (2,5 à 20 ms) ;
- 100 générations réseau simulées sans fuite de workers ;
- presets, migrations, identité, logs, bootstrap, états GUI.

`tests/conftest.py` charge `bin/opus.dll` avant tout import, comme le font `run_server.py` et `run_client.py`.

## Conventions

- **Moteurs sans Qt.** `IntercomServer` et `IntercomClient` ne dépendent pas de Qt ; les GUI ne lisent que leurs snapshots (`get_*_snapshot()`).
- **Un mixin par responsabilité.** L'état partagé est déclaré dans le `__init__` de la classe principale ; les mixins (`control.py`, `mixer.py`…) n'en créent pas.
- **Callbacks PortAudio :** ni I/O, ni codec, ni allocation, ni log. On copie dans un ring préalloué et on compte les anomalies.
- **Aucune erreur avalée en silence.** Chemin temps réel : un compteur exposé dans les diagnostics. Opération non critique : `with best_effort("fermer le socket"):`, qui journalise l'erreur. Ruff (`S110`, `S112`) bloque un `except: pass`.
- **Fail-closed.** Une donnée invalide ou une session douteuse coupe la route, elle ne l'ouvre jamais.
- **Latence bornée.** Toute nouvelle file a une capacité fixe et jette sa trame la plus ancienne.
- **Bus.** Toute règle propre à un bus passe par `common/buses.py` et `common/routing.py`.
- **Codecs.** Les moteurs instancient Opus via `opus_codec.OpusEncoder()` / `OpusDecoder()` : les tests remplacent ces deux noms dans `py_intercom.common.opus_codec`.

## Publier une version

**Pousser sur `main` suffit.** Le workflow [`.github/workflows/release.yml`](../.github/workflows/release.yml) :

1. à chaque push et pull request : lint, tests, construction des exécutables (téléchargeables comme artefact du run) ;
2. sur `main` uniquement : publication d'une release GitHub `vX.Y.N`, marquée comme la plus récente.

Le numéro de version est `X.Y` (pris dans `src/py_intercom/__init__.py`) suivi du numéro de build CI. Il s'affiche dans le titre des fenêtres. Pour passer en 2.1 ou en 3.0, modifier `__version__` dans ce fichier.

Contenu de chaque release :

| Fichier | Rôle |
|---|---|
| `py-intercom-server.exe` | Serveur Régie, fichier unique |
| `py-intercom-client.exe` | Client, fichier unique |
| `py-intercom-licenses.zip` | Licence, notices tierces, licence et manifeste Opus |
| `SHA256SUMS.txt` | Empreintes de contrôle |

Les liens `https://github.com/lfpoulain/py-intercom/releases/latest/download/<fichier>` pointent toujours vers la dernière version.

## Build local des exécutables

```powershell
& '.\scripts\build_exe.ps1'
```

Même script que la CI : il produit les quatre fichiers ci-dessus dans `dist\` à partir de [`scripts/py-intercom.spec`](../scripts/py-intercom.spec). `-Version 2.0.42` estampille une version précise.

## DLL Opus

`bin/opus.dll` (libopus 1.6.1, Deep PLC) est versionnée dans le dépôt et embarquée telle quelle dans les exécutables. Les tests vérifient que son empreinte correspond au manifeste `bin/opus-build-info.json` et qu'elle annonce bien la version 1.6.1.

Pour la reconstruire (Visual Studio Build Tools 2022 et CMake requis) :

```powershell
& '.\scripts\build_opus.ps1' -Clean
```

Le script télécharge l'archive officielle Xiph (SHA-256 vérifié), compile une DLL x64 durcie, exécute les cinq tests natifs puis réécrit la DLL et son manifeste. La provenance est documentée dans [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Roadmap

Toute décision d'architecture et tout travail ouvert sont suivis dans [roadmap.md](roadmap.md), à mettre à jour dans le même changement que le code.
