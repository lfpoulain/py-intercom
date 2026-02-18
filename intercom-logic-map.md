# Intercom Régie Twitch — Carte Logique des Flux v2.0

---

## 1. Les bus fixes

### Bus Régie (0)

Canal bidirectionnel permanent. Tout le monde en régie l'entend en continu. C'est le "canal d'équipe".

- Écoute activable par client (toggle "Écoute Régie")
- Le serveur mix les flux de tous les membres et envoie à chacun (sans son propre flux = pas d'écho)
- Les présentateurs qui répondent via micro intercom arrivent ici
- Pas d'output device externe, c'est du casque client uniquement

### Bus de Diffusion (Plateau = 1, VMix = 2)

Canal unidirectionnel vers un output device. Personne ne "s'abonne" pour écouter. C'est un "tuyau de sortie".

- Activé par PTT côté client
- Envoie le mix vers un ou plusieurs outputs (VB-Cable, etc.)
- Toggle serveur : "Renvoyer dans Régie" (défaut : oui)
- Le serveur mix les sources assignées et push vers les outputs

> **Clé :** Cette séparation résout le problème de doublons. Un client n'écoute QUE le bus Régie. Les bus de diffusion ne servent qu'à sortir du son vers des devices externes.

---

## 2. Architecture globale

### Client (chaque poste)

| Fonction | Description |
|----------|-------------|
| Input | 1 micro sélectionné |
| Mode micro | PTT (par bus uniquement) |
| Envoi | Flux audio vers le serveur (silence si aucun PTT actif) |
| Écoute | Bus Régie + Return bus si activés |
| PTT par bus | Peut PTT dans plusieurs bus de diffusion simultanément |

### Serveur (machine régie)

| Fonction | Description |
|----------|-------------|
| Réception | Tous les flux clients entrants |
| Routing | Assigne les clients aux bus (matrice checkboxes) |
| Mix Régie | Mixe les sources du bus Régie, envoie à chaque client (dédupliqué, sans son propre flux) |
| Mix Diffusion | Mixe les sources des bus de diffusion, envoie vers les outputs |
| Toggle par bus | "Renvoyer dans Régie" (oui/non) |
| Gestion dynamique | Créer/supprimer des bus et des outputs à la volée |

### Outputs (devices)

| Output | Rôle |
|--------|------|
| Casque client | Reçoit le mix du Bus Régie (+ return si activé) |
| VB-Cable 1 | Reçoit le mix du Bus Plateau → VMix |
| VB-Cable 2 | Reçoit le mix du Bus VMix → VMix |
| Autres | Tout output ajouté dynamiquement, un bus peut avoir plusieurs outputs |

---

## 3. Flux audio : de l'envoi à la réception

### Flux 1 : Client vers Serveur

```
Micro Client → [PTT actif ?] → Encode Opus → Serveur (UDP)
```

Si aucun PTT actif → le client envoie du silence (ou rien).

### Flux 2 : Serveur mix Régie vers Client

```
Serveur reçoit flux → Mix Bus Régie → Déduplique (exclut flux du destinataire) → Casque Client
```

L'assistant reçoit le mix Régie SANS son propre flux dedans (pas d'écho).

### Flux 3 : Serveur mix Diffusion vers Output

```
Serveur reçoit flux → Mix Bus Plateau → VB-Cable (output)
```

### Flux 4 : Renvoi dans Régie (optionnel)

```
Mix Bus Plateau → [Toggle "Renvoyer dans Régie" activé ?] → Copie dans Bus Régie → Casques Régie
```

Permet à l'équipe régie d'entendre ce qui est envoyé au plateau/vmix sans être abonnée directement à ces bus.

---

## 4. Logique de déduplication côté serveur

Comment le serveur construit le mix pour chaque client :

1. Le serveur reçoit les flux audio de **tous les clients connectés**
2. Pour chaque client destinataire, il identifie **quels flux il doit entendre** via le Bus Régie (tous les membres assignés au bus Régie, sauf lui-même)
3. Il vérifie si des bus de diffusion ont le toggle **"Renvoyer dans Régie"** activé. Si oui, il identifie les flux qui y transitent
4. **Déduplication :** si un flux (ex: le réal) est déjà dans le bus Régie ET dans le bus Plateau renvoyé, le serveur ne l'inclut qu'une seule fois dans le mix envoyé au client
5. Le serveur envoie **un seul flux mixé** au client (pas un flux par bus)

> **Important :** La déduplication est par client. Chaque client reçoit un mix personnalisé. Mais les outputs de bus de diffusion (VB-Cable etc.) reçoivent le mix complet sans déduplication, c'est leur rôle.

---

## 5. Matrice des responsabilités Client vs Serveur

| Action | Client | Serveur | Notes |
|--------|--------|---------|-------|
| Capture micro | ✓ | — | Encode Opus, envoie en UDP |
| Décision PTT | ✓ | — | Par bus, côté client |
| Assigner un client à un bus | — | ✓ | Matrice checkboxes sur l'UI serveur |
| Mixer les flux audio | — | ✓ | Un mix par client (Régie) + un mix par output (Diffusion) |
| Déduplication | — | ✓ | Exclut le flux propre + déduplique les renvois |
| Envoi vers outputs (VB-Cable) | — | ✓ | Le serveur sort directement sur les devices |
| Toggle "Renvoyer dans Régie" | — | ✓ | Par bus de diffusion |
| Créer / supprimer bus | — | ✓ | Dynamique, interface serveur |
| Créer / supprimer outputs | — | ✓ | Dynamique, interface serveur |
| Choisir accès client aux bus | À définir | ✓ | Le serveur assigne. Le client pourra peut-être aussi ? |
| Écouter le Bus Régie | ✓ | — | Réception du mix personnalisé depuis le serveur |
| Sauvegarder / charger presets | — | ✓ | Tout le routing + config dans un preset YAML |

---

## 6. Scénarios concrets

### Scénario A : Le réal parle au plateau pendant le live

1. Le réal appuie sur **PTT Plateau (F2)** sur son client
2. Son flux audio part au serveur `CLIENT → SERVEUR`
3. Le serveur route le flux dans **Bus Plateau** `MIX`
4. Bus Plateau sort sur **VB-Cable → oreillettes présentateurs** `OUTPUT`
5. Toggle "Renvoyer dans Régie" = ON → le serveur copie le flux dans le **Bus Régie**
6. L'assistant entend le réal parler au plateau via son casque `BUS RÉGIE`

### Scénario B : Le réal parle à la régie (PTT Régie)

1. Le réal maintient le **PTT Régie**
2. Son flux est envoyé au serveur pendant l’appui
3. Le serveur mix et envoie à **l'assistant** (sans le flux de l'assistant lui-même)
4. Le réal n'entend PAS son propre flux dans le Bus Régie

### Scénario C : Le réal parle au live (VMix) sans que le plateau sache

1. Le réal appuie sur **PTT VMix (F3)** uniquement (pas F2)
2. Le flux part dans **Bus VMix** → sort sur VB-Cable VMix
3. Le Bus Plateau ne reçoit rien → les présentateurs n'entendent rien
4. Si "Renvoyer dans Régie" est ON sur Bus VMix → l'assistant entend quand même

### Scénario D : Un présentateur répond via micro intercom pendant une séquence vidéo

1. Le présentateur parle dans son **micro oreillette (client intercom)**
2. Son flux arrive au serveur, routé dans **Bus Régie uniquement**
3. Le réal et l'assistant l'entendent dans leurs casques
4. Rien ne part dans VMix → pas de pollution du live

### Scénario E : Le réal parle dans Plateau ET VMix en même temps

1. Le réal appuie sur **F2 (Plateau) + F3 (VMix) simultanément**
2. Son flux est routé dans **les deux bus de diffusion**
3. Les deux outputs reçoivent le mix
4. L'assistant reçoit le flux **une seule fois** dans le Bus Régie (déduplication serveur)

---

## 7. Règles de routage du serveur

**Règle 1 — Exclusion du propre flux :** Quand le serveur construit le mix du Bus Régie pour un client X, il exclut le flux de X.

**Règle 2 — Déduplication intelligente :** Si un flux source est présent dans le Bus Régie ET dans un bus de diffusion renvoyé, il n'est inclus qu'une seule fois dans le mix envoyé au client.

**Règle 3 — PTT côté client :** C'est le client qui décide quand envoyer son audio. Si aucun PTT actif, le client n'envoie rien (économie réseau).

**Règle 4 — Assignment côté serveur :** Le serveur décide quel client a accès à quel bus. Le client peut PTT dans un bus seulement si le serveur l'y a autorisé.

**Règle 5 — Outputs indépendants :** Les outputs de bus de diffusion reçoivent toujours le mix complet (toutes les sources assignées). Pas de déduplication pour les outputs devices.

**Règle 6 — Renvoi optionnel :** Chaque bus de diffusion a un toggle "Renvoyer dans Régie". Quand il est activé (défaut), une copie du mix est injectée dans le Bus Régie. Quand il est désactivé, seuls les outputs reçoivent le mix.

---

## 8. Points en suspens

**Accès client aux bus :** Est-ce que le client peut aussi choisir ses bus (en plus du serveur qui les assigne) ? Ou c'est 100% géré par le serveur ?

**Mute bus côté client :** Le client n'écoute que le Bus Régie, donc un seul volume/mute global côté écoute. Suffisant ?

**Notifications visuelles :** Quand le réal PTT dans Plateau, l'assistant voit-il un indicateur visuel "Réal parle au Plateau" en plus de l'audio renvoyé ?
