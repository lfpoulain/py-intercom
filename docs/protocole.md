# Protocole V2

py-intercom utilise deux canaux par client :

- **contrôle** : TCP (port audio + 1), messages JSON ;
- **audio** : UDP (port audio, 5000 par défaut), en-tête binaire fixe + payload Opus.

Une connexion TCP seule n'ouvre jamais l'audio. Le protocole V1 est refusé partout.

## Séquence d'une session

```text
Client                                   Serveur
  │── TCP connect ─────────────────────────►│
  │── hello {version, client_id, uuid,      │  vérifie version, identité,
  │          name, udp_port} ──────────────►│  collisions ; crée le token
  │◄───────────── welcome {version, token} ──│
  │◄───────────── update {config: buses…} ───│
  │   (le client ouvre alors l'audio UDP)   │
  │── state {ptt_buses, gains, écoutes} ───►│
  │══ audio UDP (token, bus_mask) ═════════►│
  │◄═════════════ mix-minus UDP (token) ═════│
  │── ping ─────────────────────────────────►│  toutes les 50 ms
  │◄─────────────────────────── pong ────────│
```

## Contrôle TCP

Objets JSON UTF-8, un par ligne (`\n`).

| Message | Sens | Contenu |
|---|---|---|
| `hello` | client → serveur | `version`, `client_id`, `client_uuid`, `name`, `udp_port`, `applies_input_gain_locally` |
| `welcome` | serveur → client | `version`, `session_token` (16 caractères hexadécimaux minuscules), `server_time_ms` |
| `update` | serveur → client | `config` : `client_id`, `input_gain_db`, `buses`, `return_vu_dbfs` |
| `state` | client → serveur | `ptt_buses`, `listen_regie`, `listen_return_bus`, `duck_return`, `input_gain_db`, `return_gain_db` |
| `ping` / `pong` | les deux | vivacité ; le `pong` porte le VU du Return |
| `kick` | serveur → client | éjection demandée par l'opérateur |

Règles :

- `hello` doit être le premier message reçu par le serveur, `welcome` le premier reçu par le client ;
- un second `hello` ou `welcome` sur la même connexion est une erreur ;
- `client_id` doit correspondre au CRC32 de `client_uuid` et ne pas entrer en collision avec un autre UUID connu ;
- les valeurs de `state` sont typées strictement (booléens, nombres finis, bus connus) ;
- ligne ≤ 64 Kio, tampon ≤ 128 Kio, `hello` attendu sous 3 s ;
- 64 sessions simultanées au maximum ;
- une session sans trafic pendant 6 s expire, et son token est révoqué.

Toute violation ferme la connexion ; le client se reconnecte avec un backoff exponentiel (1 à 10 s).

## Audio UDP

Tous les entiers sont en ordre réseau (big-endian). L'en-tête fait exactement 32 octets.

| Offset | Taille | Champ | Règle |
|---:|---:|---|---|
| 0 | 4 | `magic` | `PYI2` |
| 4 | 1 | `version` | `2` |
| 5 | 1 | `header_size` | `32` |
| 6 | 2 | `flags` | exactement une direction : `0x0001` client → serveur, `0x0002` serveur → client |
| 8 | 8 | `session_token` | non nul, celui de la session TCP courante |
| 16 | 4 | `client_id` | source (client → serveur) ou destinataire (serveur → client) |
| 20 | 4 | `timestamp_ms` | millisecondes modulo 2³² |
| 24 | 4 | `sequence_number` | modulo 2³² |
| 28 | 2 | `stream_id` | toujours `0` en V2 |
| 30 | 2 | `bus_mask` | bit *n* = bus *n* ; `0` dans le sens serveur → client |
| 32 | 1–1200 | payload | une trame Opus de 10 ms, non vide |

### Acceptation côté serveur

Un paquet n'est accepté que si **toutes** ces conditions sont vraies :

- une session TCP vivante existe pour ce `client_id` ;
- le token est celui de cette session ;
- l'adresse IP **et** le port source sont ceux déclarés dans `hello` ;
- la direction est client → serveur ;
- `stream_id = 0` et `bus_mask` ne contient que des bus connus.

UDP ne crée jamais de client et ne modifie jamais l'adresse déclarée. Chaque rejet incrémente un compteur dédié, visible dans les diagnostics.

### Acceptation côté client

Le client n'accepte l'audio qu'après `welcome` et la première configuration. Il vérifie l'adresse et le port du serveur, la direction, son `client_id`, le token courant, le stream et le masque. À chaque perte de contrôle ou reconnexion, token, jitter buffer et files sont vidés.

## Contrôle à distance (HTTP) du client

Désactivé par défaut. Activé dans le client, il écoute sur `127.0.0.1:5080` (port réglable), ou sur toutes les interfaces si « Depuis le réseau » est coché. Toutes les réponses sont du JSON UTF-8.

| Méthode | Chemin | Effet |
|---|---|---|
| `POST` | `/ptt/<bus>/on` | Active le bus |
| `POST` | `/ptt/<bus>/off` | Désactive le bus |
| `POST` | `/ptt/<bus>/toggle` | Inverse le bus |
| `POST` | `/ptt/all/off` | Relâche tous les bus |
| `GET` | `/status` | État courant |

`<bus>` : `regie`, `plateau`, `vmix` ou `0`, `1`, `2`, insensible à la casse. Corps de requête vide.

Réponse de `/status` et des commandes réussies :

```json
{
  "state": "En ligne",
  "online": true,
  "buses": {
    "regie":   {"mode": "ptt",       "active": true},
    "plateau": {"mode": "toggle",    "active": false},
    "vmix":    {"mode": "always_on", "active": true}
  }
}
```

| Code | Cas |
|---|---|
| `200` | Commande appliquée ; l'état à jour est renvoyé |
| `409` | Client pas « En ligne » (PTT désarmé), ou `off` sur un bus en mode Always On |
| `404` | Bus ou route inconnus |
| `405` | Mauvaise méthode (commande PTT en `GET`) |
| `403` | Requête de navigateur (en-tête `Origin` ou `Referer`) |
| `503` | Interface occupée, réessayer |

Les erreurs ont la forme `{"error": "message"}` ; le `409` hors ligne inclut aussi l'état complet.

Sémantique :

- **Mode PTT** : `on`/`off` maintiennent ou relâchent un appui distant, qui s'ajoute au raccourci clavier. Un `off` distant ne coupe pas une touche tenue par l'opérateur.
- **Mode Toggle** : `on`/`off`/`toggle` fixent l'état de bascule, synchronisé avec le raccourci.
- **Mode Always On** : le bus reste actif ; `off` renvoie `409`.
- Hors ligne ou à la perte de connexion, tout appui distant est effacé.

Les commandes sont exécutées sur le thread de l'interface, par le même chemin que les raccourcis clavier.

## Découverte LAN

Le serveur diffuse toutes les 2 s une balise JSON en broadcast UDP (port audio + 2) : nom du serveur, ports audio et contrôle, version. Le client liste les serveurs vus depuis moins de 6 s.
