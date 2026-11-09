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
├── launcher.py              choix du rôle (Régie ou poste), lancement d'un poste local
├── common/                  briques partagées, sans état global
│   ├── audio.py             gains, RMS, limiteur
│   ├── audio_errors.py      messages d'erreur PortAudio lisibles
│   ├── bootstrap.py         chargement de opus.dll, rapports de crash
│   ├── buses.py             catalogue fixe des bus (source unique)
│   ├── constants.py         contrat audio, budget de latence, ports
│   ├── diagnostics.py       taux par seconde et formats des diagnostics opérateur
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
│   ├── qos.py               marquage voix de l'audio UDP (qWAVE, DSCP 56)
│   ├── realtime.py          priorité Windows (ABOVE_NORMAL, MMCSS), options de sockets
│   ├── role_dialog.py       fenêtre « Que fait ce PC ? » du premier lancement
│   ├── routing.py           routeur pur et mix-minus
│   ├── theme.py             thème Qt : couleurs, style, interrupteurs
│   ├── console.py           widgets console : vu-mètres LED, faders, pads, tranches
│   └── gui_utils.py         inventaire audio hors thread Qt, icône, lancement des fenêtres
├── server/
│   ├── server.py            IntercomServer : état partagé, cycle de vie, snapshots
│   ├── control.py           sessions TCP : hello/welcome, état, config
│   ├── mixer.py             réception UDP, mixage, broadcast
│   ├── outputs.py           sorties physiques
│   ├── return_input.py      entrée Return
│   ├── presets.py           persistance, collisions d'identité
│   ├── models.py            ClientProfile, ClientState, OutputState…
│   ├── health.py            santé opérateur dérivée des snapshots
│   ├── gui.py               fenêtre Régie (console de mixage + réglages)
│   └── main.py              point d'entrée GUI
└── client/
    ├── client.py            IntercomClient : état, cycle de vie, sessions
    ├── control.py           contrôle TCP : handshake, pings, état
    ├── transport.py         audio UDP : encodage, réception, playout
    ├── audio_io.py          streams PortAudio et callbacks
    ├── status.py            état opérateur (En ligne, Dégradé…)
    ├── hotkeys.py           raccourcis PTT globaux (pynput → Qt)
    ├── remote.py            contrôle HTTP du PTT (Stream Deck, Companion)
    ├── preflight.py         test micro, casque, réseau et raccourcis
    ├── gui.py               fenêtre client (pads de parole + réglages)
    └── main.py              point d'entrée GUI
```

`IntercomServer` et `IntercomClient` sont composés de *mixins*, un par responsabilité : chaque fichier se lit seul, et l'état partagé (verrous, compteurs, sessions) reste déclaré à un seul endroit, dans le `__init__` de la classe principale.

`run_intercom.py` reste à la racine : il rend `bin/opus.dll` visible **avant** l'import d'`opuslib`, puis passe la main à `launcher.py`. Ce dernier lit `--role server|client`, ou le rôle retenu sur ce PC, ou demande au lancement. Un seul exe porte donc les deux rôles, mais chacun tourne dans **son propre processus** : le bouton « Poste sur ce PC » de la Régie relance le même programme avec `--role client --server 127.0.0.1`. Serveur et client ne partagent ainsi ni interpréteur, ni GIL, ni plantage.

## Modèle d'exécution temps réel

Règle d'or : **aucun callback PortAudio ne fait d'I/O, n'encode, ne décode, ne prend de verrou long ni n'alloue.** Les callbacks copient des trames dans des rings préalloués ; des workers dédiés font le reste.

### Client

| Thread | Rôle |
|---|---|
| Callback d'entrée | Valide la trame, applique le gain, calcule le VU, pousse dans le ring TX |
| Worker TX | Seul propriétaire de l'encodeur Opus, de la séquence et de l'envoi UDP |
| Worker RX | Valide les datagrammes et alimente le jitter buffer |
| Worker playout | Réveillé par le callback de sortie : lit le jitter buffer 6 ms avant la lecture suivante de la carte, décode, met une trame d'avance dans le ring de lecture |
| Callback de sortie | Dépile une trame, applique le volume, joue du silence en cas de manque, et demande la trame suivante |
| Worker contrôle | Connexion TCP, handshake, pings, reconnexion avec backoff |

Le processus tourne en priorité `ABOVE_NORMAL` (jamais `HIGH` : vMix ou un encodeur doivent continuer de tourner), avec un passage du GIL toutes les 1 ms au lieu de 5. Les workers TX, RX et playout, et côté serveur `udp-rx`, `mix` et `udp-tx`, sont inscrits auprès de MMCSS comme threads « Pro Audio ». Les sockets UDP ignorent les ICMP « port injoignable » (`SIO_UDP_CONNRESET`) et le contrôle TCP envoie chaque ligne sans délai de Nagle (`TCP_NODELAY`).

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

Le catalogue `common/buses.py` est la source unique des bus. Chaque paquet audio porte son **`bus_mask`** : la route voyage avec la trame, jamais séparément. Une trame reconstruite (FEC ou PLC) reprend la route de la dernière trame reçue de la même session, jamais une autre.

`common/routing.py` est une fonction pure :

1. n'accepter que des trames `float32` mono, finies, de 480 échantillons ;
2. ne garder que les bits de bus connus ;
3. appliquer le gain de chaque bus ;
4. sommer en linéaire, sans limiter le mix partagé ;
5. construire le monitoring Régie sans compter deux fois un client ;
6. pour chaque destinataire, soustraire sa propre contribution (mix-minus) ;
7. ajouter son Return personnel, atténué par le ducking si le client l'a demandé (`common/ducking.py`) ;
8. limiter uniquement le signal final de chaque destination.

## Latence

| Étape | Contribution |
|---|---|
| Trame de capture | 10 ms |
| Pré-buffer réseau (jitter buffer) | ~30 ms par saut (`JB_START_FRAMES = 3`) ; un paquet en retard isolé au-delà est rattrapé par le FEC. ~60 ms pour un poste « en Wi-Fi » (`WIFI_JB_START_FRAMES = 6`), dans les deux sens de sa liaison seulement |
| Encodage / décodage Opus | quelques ms |
| Réseau LAN, ordonnanceur | variable |
| Buffers WASAPI et carte son | dépend du pilote |

Le test d'intégration vérifie qu'en boucle locale, sans carte son, la voix d'un client arrive chez un autre en moins de 250 ms.

**Toutes les files sont bornées et jettent leur trame la plus ancienne quand elles sont pleines** (constantes dans `common/constants.py`) : le retard ne peut jamais dépasser leur capacité, au prix d'une coupure brève. Le jitter buffer démarre sur la fenêtre la plus récente et saute en avant sur un grand trou.

**Plafond de latence.** Deux horloges ne battent jamais exactement au même rythme : carte son du client, horloge du serveur, carte son d'une sortie. Une rafale réseau peut aussi laisser des trames en trop. Côté client, la lecture est cadencée par la carte son du casque elle-même : chaque callback demande la trame suivante, le jitter buffer est donc lu exactement au rythme de la carte et cette dérive-là disparaît. Ailleurs, le jitter buffer et les rings de lecture surveillent leur profondeur : si elle reste au-dessus de sa cible pendant 0,5 s (`LATENCY_TRIM_AFTER_FRAMES`), une trame de 10 ms est retirée. Un excédent de plus de 2 trames qui dure 30 lectures (rafale réseau, lecteur bloqué) est retiré d'un coup, ou dès 10 lectures s'il dépasse 6 trames (un trou de plus de ~100 ms). Avant cela, le jitter buffer se délestera de son excédent **dans une pause** : dès que la profondeur reste de plus de 2 trames au-dessus de sa cible pendant 3 lectures (une gigue ordinaire, qui la fait flotter 1 à 2 trames au-dessus, n'est pas touchée), la prochaine trame décodée sous -50 dBFS (`JB_CATCH_UP_QUIET_DBFS`) est sautée au lieu d'être jouée (`skip_silent_slot`, côté poste comme côté Régie), si bien que la latence revient à la cible sans couper un mot. La latence revient ainsi toujours vers la cible (~20 ms de jitter buffer, ≤ 20 ms par ring), et un retard ne peut jamais s'installer. Après un silence, la lecture reprend sur la première trame attendue avec seulement la moitié du pré-buffer (`JB_RESUME_FRACTION`) : une prise de parole PTT ne démarre pas avec un tampon vide (l'attente est bornée, un paquet isolé est joué quand même), et des paquets simplement retardés, déjà en tampon, sont joués aussitôt. Au plafond de 30 trames, la plus ancienne est sautée (et non supprimée sur place), pour que la lecture ne reste pas bloquée sur des trames masquées. Chaque retrait est compté (`jb_trimmed`, `playout_ring_trimmed`, `queue_trimmed`, `return_queue_trimmed`).


## Audio et Opus

| Paramètre | Valeur |
|---|---|
| Fréquence | 48 000 Hz, mono |
| Trame | 480 échantillons (10 ms) |
| Codec | Opus 1.6.1 (`bin/opus.dll`), mode VoIP (hybride SILK + CELT), 64 kbit/s, complexité 5 |
| FEC | FEC intégré (`OPUS_EXPECTED_LOSS_PERCENT = 10`) : une trame perdue est reconstruite depuis le paquet suivant, déjà dans le jitter buffer, sans latence ajoutée |
| PLC | Deep PLC (complexité décodeur 5) quand le paquet suivant manque aussi ; DRED et OSCE désactivés |
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
- L'inventaire des périphériques tourne hors du thread Qt (`DeviceRefresh`, commun aux deux fenêtres). Un pilote peut y rester bloqué : après 5 s, la fenêtre l'indique mais attend toujours, et un inventaire tardif reste utilisé. Une fermeture demandée pendant l'inventaire attend sa fin, ou au plus ces 5 s ; au-delà, la fenêtre libère tout ce qui ne passe pas par PortAudio, enregistre son preset, puis `run_window()` vide les logs et termine le processus sans détruire le thread bloqué.

## Limites actuelles

- Trois bus fixes, un flux mono par client, pas de matrice dynamique.
- Pas de chiffrement ni d'authentification forte.
- Jitter buffer à cible fixe (pas d'adaptation à la gigue mesurée), par choix : la latence reste au plus près du temps réel.
- WASAPI exclusif non pris en charge.
- Pas encore de banc matériel ni d'endurance qualifiés : voir la [roadmap](roadmap.md).
