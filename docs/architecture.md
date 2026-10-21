# Architecture

## Vue d'ensemble

```text
 Client A                         Serveur Régie                        Client B
┌──────────────┐  UDP audio V2  ┌──────────────────────────────┐  UDP  ┌──────────────┐
│ micro → Opus ├───────────────►│ rx → jitter → décode → mixeur├──────►│ Opus → casque│
│ casque ◄─Opus│◄───────────────┤ broadcast (mix-minus) ◄──────┘       │              │
└──────┬───────┘                │ sorties physiques (Plateau, VMix)    └──────┬───────┘
       │      TCP contrôle V2   │ Return (entrée locale)                       │
       └───────────────────────►│ sessions, tokens, état PTT    ◄──────────────┘
                                └──────────────────────────────┘
```

Deux applications Qt (PySide6) pilotent deux moteurs **indépendants de Qt**, `IntercomServer` et `IntercomClient`. Les moteurs sont testables sans interface ni carte son ; il n'existe pas de mode headless public.

Ordre de priorité du produit :

1. aucune fuite de routage ni prise de parole involontaire ;
2. aucune panne silencieuse du moteur audio ;
3. une latence stable et bornée, jamais croissante ;
4. une reconnexion déterministe, sans ancien audio ;
5. une interface lisible sous pression.

## Organisation du code

```text
src/py_intercom/
├── common/                  briques partagées, sans état global
│   ├── audio.py             gains, RMS, limiteur
│   ├── audio_errors.py      messages d'erreur PortAudio lisibles
│   ├── bootstrap.py         chargement de opus.dll, rapports de crash
│   ├── buses.py             catalogue fixe des bus (source unique)
│   ├── constants.py         contrat audio, budget de latence, ports
│   ├── control_protocol.py  framing JSON du contrôle TCP, tokens
│   ├── devices.py           inventaire et validation des périphériques
│   ├── discovery.py         annonce et découverte LAN
│   ├── frame_ring.py        ring PCM préalloué (drop-oldest)
│   ├── identity.py          UUID client → identifiant réseau
│   ├── jitter_buffer.py     réordonnancement, PLC, métadonnées par trame
│   ├── jsonio.py            écriture JSON atomique avec .bak
│   ├── logging.py           logs tournants, best_effort()
│   ├── opus_codec.py        wrapper Opus strict
│   ├── packets.py           en-tête audio UDP V2
│   ├── persistence.py       writer JSON unique avec debounce
│   ├── preset_schema.py     schéma 2 et migrations
│   ├── routing.py           routeur pur et mix-minus
│   ├── theme.py             thème Qt et widgets partagés
│   └── gui_utils.py         helpers Qt
├── server/
│   ├── server.py            IntercomServer : état partagé, cycle de vie, snapshots
│   ├── control.py           sessions TCP : hello/welcome, état, config
│   ├── mixer.py             réception UDP, mixage, broadcast
│   ├── outputs.py           sorties physiques
│   ├── return_input.py      entrée Return
│   ├── presets.py           persistance, collisions d'identité
│   ├── models.py            ClientProfile, ClientState, OutputState…
│   ├── health.py            santé opérateur dérivée des snapshots
│   ├── gui.py               fenêtre Régie
│   └── main.py              point d'entrée GUI
└── client/
    ├── client.py            IntercomClient : état, cycle de vie, sessions
    ├── control.py           contrôle TCP : handshake, pings, état
    ├── transport.py         audio UDP : encodage, réception, playout
    ├── audio_io.py          streams PortAudio et callbacks
    ├── status.py            état opérateur (En ligne, Dégradé…)
    ├── hotkeys.py           raccourcis PTT globaux (pynput → Qt)
    ├── gui.py               fenêtre client
    └── main.py              point d'entrée GUI
```

`IntercomServer` et `IntercomClient` sont composés de *mixins*, un par responsabilité : chaque fichier se lit seul, et l'état partagé (verrous, compteurs, sessions) reste déclaré à un seul endroit, dans le `__init__` de la classe principale.

`run_server.py` et `run_client.py` restent à la racine : ils rendent `bin/opus.dll` visible **avant** l'import d'`opuslib`.

## Modèle d'exécution temps réel

Règle d'or : **aucun callback PortAudio ne fait d'I/O, n'encode, ne décode, ne prend de verrou long ni n'alloue.** Les callbacks copient des trames dans des rings préalloués ; des workers dédiés font le reste.

### Client

| Thread | Rôle |
|---|---|
| Callback d'entrée | Valide la trame, applique le gain, calcule le VU, pousse dans le ring TX |
| Worker TX | Seul propriétaire de l'encodeur Opus, de la séquence et de l'envoi UDP |
| Worker RX | Valide les datagrammes et alimente le jitter buffer |
| Worker playout | Cadence le jitter buffer toutes les 10 ms, décode, pousse dans le ring de lecture |
| Callback de sortie | Dépile une trame, applique le volume, joue du silence en cas de manque |
| Worker contrôle | Connexion TCP, handshake, pings, reconnexion avec backoff |

Chaque connexion réseau est une **génération** : nouvel `Event` d'arrêt, nouveaux workers, encodeur et décodeur neufs, backlog UDP purgé. Un ancien thread ne peut ni publier d'audio ni fermer le socket d'une génération plus récente.

### Serveur

| Thread | Rôle |
|---|---|
| `udp-rx` | Valide chaque paquet contre la session (voir [protocole](protocole.md)) et l'empile dans le jitter buffer du client |
| `mix` | Toutes les 10 ms : dépile, décode, route, alimente les sorties physiques et la file de broadcast |
| `udp-tx` | Construit, encode et envoie le mix-minus de chaque client |
| `ctrl-accept` + une session par client | Contrôle TCP |
| Callbacks de sortie / Return | Ring préalloué ↔ carte son |

Les workers critiques sont surveillés : une sortie inattendue arrête le moteur et devient visible dans la santé serveur.

Toute révocation de session incrémente une **époque de routage**. L'audio préparé sous une époque périmée est jeté avant envoi, et la vérification finale est atomique avec le `sendto()`.

## Identité et sessions

- `client_uuid` est l'identité persistante (preset client).
- `client_id` est la clé réseau 32 bits dérivée du UUID (CRC32). Une collision avec un autre UUID connu est refusée.
- Chaque session de contrôle reçoit un **token aléatoire de 64 bits**, renouvelé à chaque reconnexion et invalidé à l'expiration.

Le token lie le flux UDP à la session TCP. Ce n'est ni un chiffrement ni une authentification forte : le réseau est supposé de confiance.

## Routage et mix-minus

Le catalogue `common/buses.py` est la source unique des bus. Chaque paquet audio porte son **`bus_mask`** : la route voyage avec la trame, jamais séparément. Une trame reconstruite par PLC porte un masque nul et ne peut donc ouvrir aucune route.

`common/routing.py` est une fonction pure :

1. n'accepter que des trames `float32` mono, finies, de 480 échantillons ;
2. ne garder que les bits de bus connus ;
3. appliquer le gain de chaque bus ;
4. sommer en linéaire, sans limiter le mix partagé ;
5. construire le monitoring Régie sans compter deux fois un client ;
6. pour chaque destinataire, soustraire sa propre contribution (mix-minus) ;
7. ajouter son Return personnel ;
8. limiter uniquement le signal final de chaque destination.

## Latence

| Étape | Contribution |
|---|---|
| Trame de capture | 10 ms |
| Pré-buffer réseau (jitter buffer) | ~40 ms par saut (`JB_START_FRAMES = 4`) |
| Encodage / décodage Opus | quelques ms |
| Réseau LAN, ordonnanceur | variable |
| Buffers WASAPI et carte son | dépend du pilote |

Le test d'intégration vérifie qu'en boucle locale, sans carte son, la voix d'un client arrive chez un autre en moins de 250 ms.

**Toutes les files sont bornées et jettent leur trame la plus ancienne quand elles sont pleines** (constantes dans `common/constants.py`) : le retard ne peut jamais dépasser leur capacité, au prix d'une coupure brève. Le jitter buffer démarre sur la fenêtre la plus récente et saute en avant sur un grand trou.

Limite connue : il n'y a pas de correction de dérive entre les horloges des cartes son. Une horloge légèrement plus rapide remplit la file en face jusqu'à son plafond (300 ms pour le jitter buffer) avant que des trames ne soient jetées. Voir la [roadmap](roadmap.md).

## Audio et Opus

| Paramètre | Valeur |
|---|---|
| Fréquence | 48 000 Hz, mono |
| Trame | 480 échantillons (10 ms) |
| Codec | Opus 1.6.1 (`bin/opus.dll`), 64 kbit/s, complexité 5 |
| PLC | Deep PLC activé (complexité décodeur 5) ; DRED et OSCE désactivés |
| DSP interne | `numpy.float32` dans `[-1, 1]` |

Le wrapper refuse une forme, un type ou des valeurs invalides, et tout paquet vide ou surdimensionné. Un auto-test encode/décode tourne au démarrage de chaque moteur ; son résultat est visible dans les diagnostics.

## Presets

Schéma courant : `schema_version: 2`, avec `preset_type` `server` ou `client`.

- Les anciens presets sont migrés en mémoire, puis réécrits au format courant.
- Un preset d'un schéma plus récent ou du mauvais type est refusé, jamais écrasé.
- Écriture atomique : fichier temporaire, `fsync`, `os.replace`, copie de l'ancien en `.bak`.
- Un seul writer regroupe les changements rapprochés ; `flush()` à l'arrêt.

Le serveur garde des `ClientProfile` légers pour les clients hors ligne ; codecs et jitter buffers ne sont créés qu'à l'ouverture d'une session.

## Gestion des erreurs

- Une erreur dans un chemin temps réel incrémente un **compteur** exposé dans les diagnostics, et le traitement passe au silence.
- Une opération non critique (fermer un socket, rafraîchir un widget) passe par `best_effort("action")` : l'erreur est journalisée, jamais propagée.
- Ruff (`S110`, `S112`) interdit un `except` qui avale une erreur sans trace.

## Limites actuelles

- Trois bus fixes, un flux mono par client, pas de matrice dynamique.
- Pas de chiffrement ni d'authentification forte.
- Jitter buffer de profondeur fixe, pas de FEC Opus, pas de correction de dérive d'horloge.
- WASAPI exclusif non pris en charge.
- Pas encore de banc matériel ni d'endurance qualifiés : voir la [roadmap](roadmap.md).
