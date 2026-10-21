# Roadmap

> À mettre à jour dans le même changement que le code concerné. L'historique détaillé est dans `git log`.

## Vision

Un intercom LAN desktop fiable pour le live : latence bornée, comportement fail-safe, diagnostic immédiatement compréhensible par l'opérateur.

## Décisions actées

- Produit **desktop Windows uniquement** : deux applications (serveur Régie, client). Pas de web, pas de Docker, pas de mode headless public.
- **Trois bus fixes** (Régie, Plateau, VMix). La GUI n'expose pas de configuration dynamique de bus ; le catalogue et le routeur centralisés préparent une future matrice multibus.
- **Pipeline** : 48 kHz mono, trames de 10 ms, Opus 64 kbit/s avec Deep PLC.
- **Temps réel avant tout** : toutes les files sont bornées et jettent l'audio le plus ancien. On accepte une coupure brève plutôt qu'un retard qui s'accumule.
- **LAN de confiance**, sans pour autant accepter de client UDP anonyme ni d'état fail-open.
- Les endpoints WDM-KS natifs à 8, 16 ou 44,1 kHz restent exclus tant qu'il n'existe pas d'adaptateur de fréquence borné, hors callbacks.

## Acquis

- Sûreté live (P0) : codec protégé, sessions UDP liées au token TCP, routage fail-closed par trame, mix-minus, reconnexion par génération, changements de périphérique transactionnels, PTT relâchés en cas de doute.
- Architecture (P1) : catalogue de bus, routeur pur, protocole V2, profils séparés des sessions, presets versionnés et atomiques, logs tournants, santé par facette, design system Qt commun, runtime Opus reproductible.
- Octobre 2026 : test d'intégration client/serveur de bout en bout, moteurs et GUI découpés par responsabilité, lint ruff en CI, plus aucune exception avalée sans trace.
- Octobre 2026 : plafond de latence (dérive d'horloge et rafales absorbées), préflight opérateur, ducking du Return en option.

## Travaux ouverts

### Moteur temps réel

- [ ] Mesurer sur le banc matériel la fréquence des retraits de trame ; s'ils sont audibles, passer à un rééchantillonnage léger.
- [ ] Étudier un adaptateur de fréquence 8/16/44,1 kHz ↔ 48 kHz, hors callbacks.

### Observabilité

- [ ] Compter les paquets réordonnés et dupliqués.
- [ ] Export de diagnostic en un clic.

### Interface opérateur

- [ ] Revue visuelle aux résolutions et facteurs DPI de production.
- [ ] Séparer en écrans distincts si les essais opérateur le justifient.

### Industrialisation

- [ ] Signature Authenticode des exécutables et procédure de versionnement des releases.

## Qualification matérielle (non acquise)

À exécuter avant tout usage en production :

- Ethernet et Wi-Fi représentatifs ; cartes son et VB-Cable réellement utilisés ;
- 2, 5 puis 10 clients ;
- latence en boucle P50 / P95 / P99 ;
- endurance de quatre heures minimum ;
- hotplug, perte réseau, redémarrage serveur, reconnexion ;
- comptage automatique des discontinuités, xruns, PLC et resynchronisations.
