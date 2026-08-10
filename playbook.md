# Playbook — py-intercom

> Dernière mise à jour : 7 août 2026
>
> Ce document décrit l'implémentation desktop V2. Les travaux non terminés et les critères de qualification sont suivis dans [roadmap-v2.md](roadmap-v2.md).

## 1. Produit et périmètre

`py-intercom` est un intercom audio temps réel pour LAN composé de deux applications graphiques Windows.

| Composant | Rôle |
|---|---|
| Serveur Régie | Reçoit les clients, route leurs prises de parole, alimente les sorties physiques et renvoie un mix-minus individuel |
| Client | Capture le micro, transmet en Opus, reçoit le mix-minus et le joue au casque |

Points d'entrée utilisateur :

```text
run_server.py  → GUI serveur
run_client.py  → GUI client
```

Le produit web, Docker et le mode console sans GUI ont été retirés. Les moteurs `IntercomServer` et `IntercomClient` restent indépendants de Qt afin d'être testables, mais ils ne constituent pas des interfaces utilisateur headless publiques.

Périmètre fonctionnel actuel :

- un serveur unique sur le LAN ;
- clients desktop PySide6 ;
- trois bus fixes : Régie, Plateau et VMix ;
- PTT par bus avec les modes `PTT`, `Toggle` et `Always On` ;
- raccourcis clavier globaux via `pynput` ;
- écoute Régie et Return configurables séparément ;
- sorties physiques serveur associées à un bus et à un gain ;
- presets JSON client et serveur ;
- contrôle TCP V2, audio UDP V2 et découverte LAN.

Le modèle fixe répond au besoin actuel. Le catalogue commun et le routeur pur évitent néanmoins de disperser les règles dans les GUI et préparent une future matrice multibus/multipiste.

## 2. Identité et sessions

- `client_uuid` est l'identité persistante enregistrée dans le preset client.
- `client_id` est la clé réseau 32 bits dérivée du UUID par `crc32`.
- Le protocole V2 exige que le client déclare son UUID et son identifiant dans `hello` ; le serveur vérifie leur correspondance canonique.
- L'identifiant réservé `0`, un UUID vide/invalide ou une collision CRC32 avec un autre UUID connu entraînent le refus de la session et un compteur de diagnostic.
- Le serveur crée un token aléatoire non nul de 64 bits pour chaque nouvelle session de contrôle.
- Le token est renouvelé lors d'un remplacement ou d'une reconnexion et invalidé dès que la session expire.

Le token empêche un ancien flux UDP ou un datagramme anonyme de rejoindre une session courante. Il ne fournit ni chiffrement ni authentification cryptographique : le réseau reste considéré comme un LAN de confiance.

## 3. Catalogue de bus et routage

Le catalogue immuable de `common/buses.py` est la source unique des identifiants, noms, couleurs et valeurs par défaut.

| ID | Nom | Usage actuel | `feed_to_regie` par défaut |
|---:|---|---|---|
| 0 | Régie | Communication et monitoring client | désactivé, car Régie ne peut pas s'alimenter elle-même |
| 1 | Plateau | Diffusion vers une sortie physique | désactivé |
| 2 | VMix | Diffusion vers une sortie physique | désactivé |

Une prise de parole transporte un `bus_mask` dans **chaque paquet audio**. Les bits inconnus n'ouvrent jamais une route. Le serveur associe ce masque à la séquence et au payload Opus dans le jitter buffer ; il ne consulte donc pas un état PTT TCP plus récent ou plus ancien pour router la frame.

`common/routing.py` applique les règles suivantes :

1. accepter seulement des frames mono `float32`, finies et longues de 480 échantillons ;
2. filtrer le masque sur les bus du catalogue ;
3. appliquer le gain master de chaque bus ;
4. sommer les contributions en domaine linéaire, sans limiter le mix partagé ;
5. construire le monitoring Régie sans compter deux fois un même client ;
6. calculer chaque mix-minus en soustrayant la contribution exacte du destinataire ;
7. ajouter le Return individuel ;
8. limiter seulement le signal final de chaque destination.

Si une source vise Régie et un autre bus alimentant Régie, sa route Régie directe est prioritaire. Sinon, le premier bus `feed_to_regie` dans l'ordre du catalogue fournit son unique contribution au monitoring. Cette règle conserve le comportement actuel ; une future matrice additive devra l'expliciter autrement.

### Return bus

Le serveur peut capturer une entrée locale, par exemple une sortie programme via VB-Cable. Cette frame est ajoutée au retour uniquement si le client est abonné, avec son `return_gain_db` personnel.

### Outputs serveur

Un output associe un périphérique `sounddevice`, un `bus_id`, un gain, un état de stream, un VU et des compteurs d'underflow. Les périphériques doivent accepter le contrat 48 kHz mono.

## 4. Audio et Opus

| Paramètre | Valeur nominale |
|---|---|
| Sample rate | 48 000 Hz |
| Canaux | 1, mono |
| Frame moteur | 480 échantillons, 10 ms |
| Codec | Opus via `opuslib` et `bin/opus.dll` |
| Bitrate | 64 kbit/s |
| Complexité | 5 |
| DSP interne | `numpy.float32` dans `[-1, 1]` |

`common/opus_codec.py` valide la configuration avant d'appeler la bibliothèque native. L'encodeur refuse une forme incorrecte, des valeurs non finies, un payload vide ou un payload supérieur à la limite Opus. Le décodeur expose toujours le contrat de taille demandé : un résultat plus court est complété par du silence et un résultat plus long est tronqué ; les moteurs appliquent en plus leur contrat fixe de 480 échantillons et remplacent une frame invalide par du silence ou la rejettent sans tuer le worker.

Les tests couvrent avec le backend natif les durées Opus 2,5, 5, 10 et 20 ms, le PLC, les paquets corrompus et les écarts de durée. Le transport de production reste volontairement fixé à 10 ms.

Un préflight encode/décode est effectué à l'initialisation du moteur. Son état et la version `opuslib` sont exposés dans les diagnostics.

## 5. Protocole audio UDP V2

Tous les champs numériques utilisent l'ordre réseau. Le header est strictement long de 32 octets :

| Offset | Taille | Champ | Règle |
|---:|---:|---|---|
| 0 | 4 | `magic` | `PYI2` |
| 4 | 1 | `version` | `2` |
| 5 | 1 | `header_size` | `32` |
| 6 | 2 | `flags` | exactement une direction : client→serveur ou serveur→client |
| 8 | 8 | `session_token` | entier non nul, propre à la session TCP |
| 16 | 4 | `client_id` | destinataire ou source selon la direction |
| 20 | 4 | `timestamp_ms` | compteur millisecondes modulo 32 bits |
| 24 | 4 | `sequence_number` | séquence modulo 32 bits |
| 28 | 2 | `stream_id` | réservé à `0` en V2 |
| 30 | 2 | `bus_mask` | routes de cette frame ; `0` pour un retour serveur |
| 32 | 1–1200 | payload | paquet Opus non vide |

Le runtime n'effectue aucun fallback vers l'ancien header de 12 octets. Une version, une direction, un token, une taille, un stream ou un masque incompatibles entraînent un rejet comptabilisé.

### Validation client → serveur

Le serveur exige simultanément :

- une session TCP connue et vivante ;
- le token courant ;
- le bon `client_id` ;
- l'adresse IP et le port UDP déclarés dans `hello` ;
- la direction client→serveur ;
- `stream_id = 0` ;
- un masque limité aux bus fixes.

UDP ne crée jamais de client et ne peut pas remplacer son endpoint. Un client sans session de contrôle vivante n'est pas non plus une destination de broadcast.

### Validation serveur → client

Le client n'ouvre la réception audio qu'après `welcome` puis réception de la configuration initiale. Il vérifie l'adresse IP et le port serveur, la direction, son identifiant, le token courant, le stream et le masque. À chaque perte du contrôle ou reconnexion, token, jitter buffer et rings de la session précédente sont vidés.

## 6. Contrôle TCP V2

Les messages sont des objets JSON UTF-8 terminés par `\n`.

| Message | Direction | Rôle |
|---|---|---|
| `hello` | client → serveur | Version, identité, nom et port UDP |
| `welcome` | serveur → client | Version et token de session encodé sur 16 caractères hexadécimaux minuscules |
| `update` | serveur → client | Gains, catalogue de bus et configuration initiale |
| `state` | client → serveur | PTT affiché, abonnements et gains client |
| `ping` / `pong` | bidirectionnel | Liveness et VU Return |
| `kick` | serveur → client | Fermeture demandée par l'opérateur |

Règles de framing et de cycle de vie :

- `hello` doit être le premier message côté serveur ;
- `welcome` doit être le premier message côté client ;
- la version 1 est explicitement refusée ;
- taille maximale d'une ligne : 64 Kio ;
- buffer cumulé maximal : 128 Kio ;
- délai maximal pour recevoir `hello` : 3 s ;
- maximum : 64 sessions de contrôle simultanées ;
- timeout de liveness : 6 s.

Une connexion TCP établie n'est donc pas synonyme de session audio prête.

## 7. Jitter buffer, métadonnées et PLC

`OpusPacketJitterBuffer` stocke un objet atomique par séquence : payload Opus, token, identité, timestamp, stream, masque de bus et direction.

- prébuffer nominal : `JB_START_FRAMES = 4`, environ 40 ms ;
- profondeur maximale : `JB_MAX_FRAMES = 30`, environ 300 ms ;
- gestion du wrap des séquences 32 bits ;
- démarrage sur la fenêtre récente plutôt que sur tout l'historique ;
- drop des anciennes frames pour ne pas accumuler une latence croissante ;
- fast-forward sur un grand trou ;
- PLC Opus local sur un petit trou.

Une frame PLC utilise un payload vide uniquement à l'intérieur du moteur. Elle reçoit des métadonnées nulles, notamment `bus_mask = 0`, et ne peut donc pas déclencher une prise de parole. Un payload UDP vide est interdit.

La profondeur est fixe. Il n'y a actuellement ni estimation adaptative du jitter, ni FEC Opus, ni correction de dérive entre horloges audio.

## 8. Modèle d'exécution temps réel

### Client

- le callback d'entrée valide la frame, applique le gain, calcule le VU et pousse une copie dans un ring préalloué ;
- le worker TX est l'unique propriétaire de l'encodeur Opus, de la séquence et de l'envoi UDP ;
- le worker RX valide les datagrammes et alimente le jitter buffer ;
- le worker de playout cadence le jitter buffer, décode Opus et publie du PCM dans un ring ;
- le callback de sortie dépile une frame PCM, applique le gain local et produit du silence en cas d'underflow.

### Serveur

- un worker reçoit et valide l'UDP ;
- un worker de mixage décode, route les sources et alimente les destinations ;
- un worker de broadcast construit, encode et envoie les mix-minus ;
- un worker accepte le contrôle TCP et supervise les sessions ;
- le callback Return publie des frames fixes dans un ring préalloué et borné ;
- le worker de mix pousse chaque destination physique dans son propre ring ;
- le callback de chaque output dépile dans un scratch préalloué, applique le gain et produit du silence en cas d'underflow.

`FrameRingBuffer` préalloue ses frames, copie avec `numpy.copyto` et applique une politique drop-oldest lorsque le producteur dépasse le consommateur. Il remplace les anciennes concaténations de tableaux sur les sorties serveur. Les opérations codec et les sockets sont hors callbacks. Les workers critiques serveur sont suivis ; une sortie inattendue devient visible dans les statistiques et arrête le moteur.

La reconnexion client utilise une génération et un `Event` propres. Un ancien thread ne peut pas fermer le socket ou publier l'audio d'une nouvelle génération. Encodeur et décodeur Opus sont reconstruits par leurs workers pour chaque epoch audio, avant toute publication. Le backoff est interruptible, le backlog UDP est drainé et le redémarrage est refusé si d'anciens workers restent vivants.

## 9. Périphériques et GUI

L'énumération des périphériques est sérialisée. Un refresh PortAudio lourd n'est pas lancé pendant que l'audio tourne, et les sélections critiques sont verrouillées pendant une session. Les périphériques choisis sont validés en 48 kHz mono avant ouverture.

Les changements d'output et de Return côté serveur sont transactionnels : le stream candidat est ouvert avant la bascule et l'ancien état reste actif si cette ouverture échoue. La désactivation du Return ferme son stream et vide son ring.

Les workers Qt sont arrêtés et joints sans `QThread.terminate()`. Les callbacks `pynput` ne modifient pas directement les widgets : ils publient un état vers la boucle Qt. Une perte de focus ou une sortie de l'état En ligne relâche les PTT actifs.

### État client

| État | Signification |
|---|---|
| Déconnecté | aucune session demandée |
| Connexion | TCP, `welcome` ou configuration encore attendu |
| En ligne | contrôle, UDP récent, workers et streams audio actifs |
| Dégradé | session établie mais audio, UDP ou worker incomplet ; PTT désarmé |
| Reconnexion | une session auparavant en ligne tente de revenir |

La structure et les statuts sont rafraîchis toutes les 250 ms ; les VU environ toutes les 67 ms, soit 15 Hz.

### Santé serveur

La GUI distingue quatre facettes : moteur/workers, contrôle TCP, trafic UDP et streams audio. La structure est rafraîchie toutes les 500 ms et les VU/santés toutes les 67 ms. Les infobulles détaillent la cause d'un état dégradé ou en erreur.

Les fenêtres de diagnostic exposent notamment l'âge du dernier contrôle et du dernier UDP, l'état des workers et streams, les profondeurs de files, les underflows, les erreurs socket et codec, les paquets invalides et les informations Opus.

## 10. Presets

| Application | Chemin |
|---|---|
| Serveur | `~\py-intercom\server_preset.json` |
| Client | `~\py-intercom\client_preset.json` |

Le schéma courant est `schema_version: 2`, accompagné de `preset_type: "server"` ou `"client"`.

- les presets historiques sont migrés en mémoire ; la sauvegarde suivante les estampille au format courant ;
- un preset d'un schéma plus récent ou du mauvais type est refusé, pas écrasé ;
- absence, JSON invalide et erreur I/O sont distingués dans la GUI ;
- l'écriture utilise un fichier temporaire dans le même dossier, `fsync`, puis `os.replace` ;
- la version précédente est copiée vers `.bak` ;
- un seul writer non-daemon sérialise et regroupe les changements rapprochés ;
- `flush()` est exécuté à l'arrêt.

Le serveur persiste des `ClientProfile` légers pour les clients hors ligne. Il ne crée les codecs et jitter buffers qu'au moment d'une session live.

## 11. Logs et crash reports

Les logs sont écrits dans :

```text
%LOCALAPPDATA%\py-intercom\logs\client.log
%LOCALAPPDATA%\py-intercom\logs\server.log
```

En l'absence de `%LOCALAPPDATA%`, le fallback est `~\py-intercom\logs`. Rotation : 10 Mo ; rétention : 14 jours ; archives : ZIP. Le mode `--debug` ajoute aussi une console et élève le niveau de verbosité.

Une exception non gérée au bootstrap produit :

```text
~\py-intercom\client_crash.txt
~\py-intercom\server_crash.txt
```

## 12. Organisation du code

```text
src/py_intercom/
├── common/
│   ├── audio.py              # gains, PCM, RMS et limiteur
│   ├── bootstrap.py          # DLL Windows et crash reports
│   ├── buses.py              # catalogue fixe
│   ├── control_protocol.py   # framing TCP V2 et tokens
│   ├── devices.py            # inventaire et validation audio
│   ├── discovery.py          # beacon LAN
│   ├── frame_ring.py         # rings PCM préalloués
│   ├── identity.py           # UUID et identifiant réseau
│   ├── jitter_buffer.py      # séquences, métadonnées et PLC
│   ├── jsonio.py             # JSON atomique et backup
│   ├── logging.py            # logs tournants
│   ├── opus_codec.py         # wrapper Opus strict
│   ├── packets.py            # enveloppe UDP V2
│   ├── persistence.py        # writer JSON sérialisé
│   ├── preset_schema.py      # schéma 2 et migration
│   ├── routing.py            # routeur pur et mix-minus
│   └── theme.py              # thème et widgets partagés
├── client/
│   ├── client.py             # moteur audio/réseau
│   ├── gui.py                # fenêtre et FSM opérateur
│   └── main.py               # entrée GUI
└── server/
    ├── models.py             # profil persistant
    ├── server.py             # moteur audio/réseau/routage
    ├── gui.py                # fenêtre et santé opérateur
    └── main.py               # entrée GUI

run_client.py                 # bootstrap desktop client
run_server.py                 # bootstrap desktop serveur
tests/                        # tests sans matériel audio requis
```

## 13. Lancement, build et validation

### Lancement

Le lancement depuis les sources exige Python 3.11 ou plus récent.

```powershell
.\.venv\Scripts\python run_server.py
.\.venv\Scripts\python run_client.py
```

Options : `--debug` et `--minimized` uniquement.

### Tests

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests -q
```

La suite automatise les contrats audio, les durées Opus, le protocole et les tokens V2, la sécurité UDP, le routage/mix-minus, le jitter/PLC, les rings, 100 générations réseau simulées sans fuite de workers, la reconnexion, le cycle de vie GUI, les presets, les logs et le bootstrap. Les tests GUI s'exécutent hors écran.

### Exécutables

```powershell
& '.\exe scripts\build_exe.ps1'
& '.\exe scripts\build_exe.ps1' -Clean
```

PyInstaller est verrouillé dans `requirements-build.txt`. Le workflow Windows installe les dépendances, compile les sources, exécute les tests, construit `dist/server.exe` et `dist/client.exe`, puis archive les exécutables avec les notices.

`requirements.txt` verrouille le runtime, `requirements-dev.txt` pytest et `requirements-build.txt` PyInstaller. Les quatre lanceurs Windows partagent `_launch.bat`, qui contrôle Python 3.11+, crée le virtualenv si nécessaire et vérifie l'installation des dépendances.

Le package Python déclare son layout `src`, ses dépendances et ses images dans `pyproject.toml`. Les lanceurs racine restent nécessaires pour charger `opus.dll` avant l'import des modules audio.

Avant toute diffusion externe, lire [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). La provenance de la DLL Opus actuelle est un bloqueur de release publique.

## 14. Workflow opérateur

1. Lancer le serveur sur la machine Régie.
2. Configurer les sorties physiques et vérifier leur bus.
3. Configurer et tester le Return si nécessaire.
4. Lancer chaque client, puis sélectionner serveur, micro et casque.
5. Attendre l'état **En ligne** ; un simple TCP connecté ne suffit pas.
6. Vérifier les VU, l'âge UDP et les facettes de santé serveur.
7. Tester chaque PTT, route et mix-minus avant le live.
8. En cas d'anomalie, relever les diagnostics et conserver les logs avant de redémarrer.

## 15. Budget de latence

Les principales contributions sont :

- frame de capture : 10 ms ;
- prébuffer réseau nominal : environ 40 ms ;
- encode/décode Opus ;
- ordonnanceur et réseau LAN ;
- buffers du driver WASAPI et de la carte son ;
- files internes bornées.

Ces valeurs ne garantissent pas la latence de bout en bout. La charge CPU, le Wi-Fi, les drivers et les horloges des interfaces audio peuvent ajouter délai, jitter ou xruns. La qualification matérielle doit mesurer P50/P95/P99, pertes, reorder, PLC, underflows, profondeur de files et resynchronisations.

## 16. Dépannage

### Le client ne passe pas En ligne

- vérifier l'adresse du serveur et les ports TCP 5001 / UDP 5000 ;
- consulter le détail de l'état : contrôle, `welcome`, configuration, UDP, workers, micro ou casque ;
- vérifier les logs client et serveur ;
- confirmer que client et serveur utilisent tous deux le protocole V2.

### Aucun son côté client

- vérifier les abonnements Régie et Return ;
- vérifier micro, casque et format 48 kHz mono ;
- observer les VU et l'âge du dernier UDP ;
- relever les compteurs de paquet, token, direction, route et décodage rejetés.

### Underflows ou audio haché

- relever les statuts PortAudio, profondeurs de rings, PLC et underflows ;
- vérifier la charge CPU et le Wi-Fi ;
- comparer avec Ethernet et une interface audio USB connue ;
- ne pas masquer le problème en augmentant tous les buffers avant d'avoir mesuré son origine.

### Return absent

- vérifier son activation et son périphérique côté serveur ;
- vérifier l'abonnement et le gain côté client ;
- observer le VU Return et l'état du stream Return ;
- vérifier qu'aucune autre application ne monopolise le périphérique.

### Bluetooth

Un casque Bluetooth classique présente plusieurs endpoints. Pour un duplex micro + casque, choisir l'entrée et la sortie **mains-libres/HFP** du même appareil via une API Windows partagée validée par l'application. La qualité sera mono et inférieure à l'écoute A2DP. Ne pas combiner une sortie A2DP avec le micro HFP du même casque : l'ouverture du micro provoque une bascule de profil qui peut invalider la sortie.

Les menus indiquent le nom, l'API, l'index PortAudio et la fréquence native. Si la variante voulue n'est pas visible, activer « Tous les périphériques » ; ce choix est conservé. Une variante WDM-KS native à 8/16/44,1 kHz est volontairement exclue si le pilote refuse le contrat moteur 48 kHz mono.

Pour une exploitation critique, préférer USB/jack. Pour conserver la sortie stéréo A2DP, utiliser un microphone séparé ; la désactivation de la téléphonie mains-libres Windows ne convient qu'à ce scénario sans micro Bluetooth.

## 17. Limites et qualification restante

- trois bus fixes et un stream mono par client ;
- aucune matrice ou création dynamique de bus dans la GUI ;
- pas de chiffrement ni d'authentification forte ;
- jitter buffer fixe, sans adaptation au réseau ;
- pas de FEC Opus ;
- pas de correction explicite de dérive entre horloges audio ;
- WASAPI exclusive non pris en charge ;
- provenance de `bin/opus.dll` non documentée, donc release publique bloquée ;
- banc matériel, scénarios 2/5/10 clients et endurance d'un live complet non encore qualifiés.

Le suivi détaillé, les critères de sortie et le journal des changements se trouvent dans [roadmap-v2.md](roadmap-v2.md).
