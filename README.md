# py-intercom

Intercom audio temps réel sur LAN pour les équipes de production live. Le produit comprend deux applications desktop PySide6 :

- le **serveur Régie**, qui reçoit, route et redistribue l'audio ;
- le **client**, qui capture le micro et restitue un mix-minus au casque.

Il n'existe plus de produit web ni de mode headless public. Les lanceurs démarrent toujours une interface graphique ; les variantes `debug` restent des GUI avec une console de diagnostic.

## État actuel

- Trois bus fixes issus d'un catalogue commun : **Régie** (0), **Plateau** (1) et **VMix** (2).
- PTT par bus avec les modes `PTT`, `Toggle` et `Always On`, plus raccourcis clavier globaux côté client.
- Routage centralisé dans un moteur pur, validation fail-closed des routes et mix-minus calculé avant la limitation de crête.
- Return bus capturé sur une entrée audio locale du serveur et ajouté individuellement aux clients abonnés.
- Sorties physiques serveur configurables par périphérique, bus et gain.
- Audio Opus 48 kHz mono, 64 kbit/s, frames nominales de 10 ms.
- Callbacks PortAudio alimentés par des rings PCM préalloués et bornés ; codec et sockets sont exécutés par des workers dédiés.
- Transport audio **V2** sur UDP : header strict de 32 octets, token de session 64 bits et `bus_mask` attaché à chaque frame.
- Contrôle **V2** sur TCP : messages JSON délimités par ligne, handshake obligatoire et limites de taille.
- Jitter buffer Opus avec métadonnées alignées par séquence et PLC local fail-closed.
- Reconnexion isolée par génération, ancien backlog UDP purgé et anciens tokens révoqués.
- États opérateur explicites côté client : Déconnecté, Connexion, En ligne, Dégradé et Reconnexion. Le PTT est désarmé hors état En ligne.
- Santé serveur séparée en moteur, contrôle, UDP et audio.
- Presets JSON de schéma 2, migration des anciens presets, sauvegarde atomique avec `.bak` et écritures sérialisées avec debounce.
- Logs persistants tournants pour les exécutables sans console.

Le périmètre de bus reste volontairement fixe pour l'usage actuel. Le catalogue et le moteur de routage posent une base claire pour une future matrice multibus/multipiste, sans exposer aujourd'hui une configuration dynamique non qualifiée.

## Sécurité de fonctionnement

Une connexion TCP seule ne rend pas l'audio actif. Le client attend successivement `welcome`, un token valide et la configuration initiale. Le serveur n'accepte ensuite un datagramme audio que si la session de contrôle est vivante et si direction, token, identité, endpoint UDP, stream réservé et masque de bus correspondent à la session courante.

Un paquet ancien, anonyme, mal formé ou provenant d'une autre adresse est rejeté et comptabilisé. Une frame PLC porte un masque de bus nul : une perte réseau ne peut donc pas ouvrir une route par accident. Le protocole V1 n'est pas accepté par les moteurs V2.

Le token lie l'UDP à la session TCP mais ne constitue pas un chiffrement ni une authentification forte. py-intercom reste destiné à un LAN de confiance.

## Modèle de gains

| Niveau | Portée | Rôle |
|---|---|---|
| `input_gain_db` | Client + serveur | Talk gain partagé par client |
| gain du bus | Serveur | Master de Régie, Plateau ou VMix |
| gain de l'output | Serveur | Niveau d'une sortie physique |
| `return_gain_db` | Serveur, par client | Niveau personnel du Return bus |
| `output_gain_db` | Client | Volume casque local |

## Prérequis

- Windows ;
- Python 3.11 ou plus récent pour un lancement depuis les sources ;
- `bin/opus.dll`, chargé par le bootstrap commun de `run_server.py` et `run_client.py`.

L'origine de la DLL Opus actuellement embarquée n'est pas suffisamment traçable pour une redistribution publique. Voir [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) avant toute release externe.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Lancement

Les scripts de `lunch script/` créent le virtualenv et installent les dépendances au besoin.

| Script | Description |
|---|---|
| `lancer_serveur_gui.bat` | GUI serveur sans console |
| `lancer_client_gui.bat` | GUI client sans console |
| `lancer_serveur_debug.bat` | GUI serveur avec console de diagnostic |
| `lancer_client_debug.bat` | GUI client avec console de diagnostic |

Lancement manuel :

```powershell
.\.venv\Scripts\python run_server.py
.\.venv\Scripts\python run_client.py
```

Les seules options publiques sont `--debug` et `--minimized` :

```powershell
.\.venv\Scripts\python run_server.py --debug --minimized
.\.venv\Scripts\python run_client.py --debug --minimized
```

## Build Windows

Le build utilise une version verrouillée de PyInstaller et produit deux exécutables `onefile` :

```powershell
& '.\exe scripts\build_exe.ps1'
```

L'option `-Clean` supprime d'abord les dossiers `build/` et `dist/` :

```powershell
& '.\exe scripts\build_exe.ps1' -Clean
```

Résultats : `dist\client.exe` et `dist\server.exe`, accompagnés de `LICENSE`, `README.md` et `THIRD_PARTY_NOTICES.md`. Le workflow Windows compile les sources et exécute les tests avant le build et la création de l'archive.

## Ports

| Service | Transport | Port par défaut |
|---|---|---:|
| Audio Opus V2 | UDP | 5000 |
| Contrôle V2 | TCP | 5001 |
| Découverte LAN | UDP broadcast | 5002 |

## Presets et diagnostics

- Serveur : `~\py-intercom\server_preset.json`
- Client : `~\py-intercom\client_preset.json`
- Sauvegarde précédente : même chemin avec le suffixe `.bak`
- Logs : `%LOCALAPPDATA%\py-intercom\logs\server.log` et `client.log`
- Rapports de crash : `~\py-intercom\server_crash.txt` et `client_crash.txt`

Les logs tournent à 10 Mo, sont conservés 14 jours et les archives sont compressées. Si `%LOCALAPPDATA%` n'est pas disponible, les logs utilisent `~\py-intercom\logs`.

## Tests

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests -q
```

La suite couvre notamment le protocole V2, les tokens obsolètes, les routes par frame, le mix-minus, les jitter buffers et PLC, les durées Opus 2,5/5/10/20 ms, les rings, 100 générations réseau simulées, les presets, l'identité, les états GUI et le bootstrap. Elle ne nécessite pas de périphérique audio physique.

La qualification n'est toutefois pas terminée sur un banc matériel représentatif : cartes son de production, Wi-Fi dégradé, charge CPU, 2/5/10 clients et endurance d'un live complet restent à mesurer. Le jitter adaptatif, la correction de dérive d'horloge et le FEC Opus ne sont pas implémentés.

## Bluetooth

Le pipeline intercom reste en 48 kHz mono. Sous Windows, un casque Bluetooth classique expose généralement une sortie stéréo A2DP et une paire micro/sortie mains-libres HFP. Pour utiliser le micro **et** le casque ensemble, sélectionner les deux endpoints HFP via une API Windows partagée que py-intercom a validée à 48 kHz ; ne pas associer la sortie A2DP au micro HFP du même casque.

Les sélecteurs affichent le nom, l'API audio, l'index et la fréquence native. L'option « Afficher tous les périphériques » est mémorisée afin de conserver une variante DirectSound/MME fonctionnelle. Les endpoints WDM-KS limités nativement à 8/16/44,1 kHz restent filtrés tant qu'un véritable adaptateur de fréquence n'est pas intégré.

Pour un live critique, préférer un casque USB/jack. Pour conserver la qualité stéréo A2DP, utiliser un microphone séparé.

## Documentation et licence

- [playbook.md](playbook.md) : architecture, protocoles, exécution, exploitation et dépannage.
- [roadmap-v2.md](roadmap-v2.md) : décisions, qualification et journal des améliorations à tenir à jour.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) : dépendances, licences et provenance du runtime natif.

Le projet est distribué sous licence **CC BY-NC-ND 4.0**. Voir [LICENSE](LICENSE).
