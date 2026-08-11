# Roadmap V2 — py-intercom

> Registre vivant du produit. À mettre à jour dans la même modification que chaque correctif, refactorisation ou décision d'architecture.
>
> Dernière mise à jour : 10 août 2026

## 1. Vision produit

Construire un intercom LAN desktop fiable pour les lives, avec une latence bornée, un comportement fail-safe et un diagnostic opérateur immédiatement compréhensible.

Le produit doit privilégier, dans cet ordre :

1. aucune fuite de routage ou prise de parole involontaire ;
2. aucune panne silencieuse du moteur audio ;
3. une latence stable plutôt qu'une latence minimale fragile ;
4. une reconnexion déterministe sans ancien audio ;
5. une interface exploitable sous pression ;
6. une architecture testable et extensible.

## 2. Décisions actées

Ces décisions restent valides jusqu'à modification explicite de cette section.

- Produit **desktop uniquement** : le client web, Docker et leurs dépendances ont été supprimés.
- Deux applications utilisateur : GUI serveur et GUI client.
- Aucun mode headless public. Les moteurs non-UI restent séparés de Qt pour les tests.
- Trois bus fixes pour l'usage actuel : Régie (0), Plateau (1), VMix (2).
- La GUI ne doit pas exposer de configuration dynamique de bus en V2.
- Le code doit néanmoins centraliser la définition des bus et éviter les cas spéciaux dispersés afin de préparer une future matrice multibus/multipiste.
- Pipeline nominal : 48 kHz, mono, frames de 480 échantillons / 10 ms, Opus 64 kbit/s.
- Les APIs audio partagées de Windows peuvent adapter un périphérique au contrat 48 kHz. Un endpoint natif WDM-KS 8/16/44,1 kHz reste exclu tant qu'un adaptateur de fréquence explicite, borné et hors callback n'est pas intégré.
- Réseau de production considéré comme LAN de confiance, sans que cela autorise les clients UDP anonymes ou les états fail-open.

## 3. Règles de mise à jour

Pour chaque changement :

1. référencer l'élément de roadmap concerné ;
2. ajouter ou adapter un test de non-régression lorsque le comportement est testable ;
3. documenter toute nouvelle métrique ou option opérateur ;
4. ne passer un item à `Terminé` qu'après validation ;
5. inscrire la date et le résultat dans le journal en fin de document.

États utilisés : `À faire`, `En cours`, `Bloqué`, `Terminé`, `Reporté`.

## 4. P0 — sûreté live

### P0.1 Codec et frames audio — Terminé (validation logicielle)

- [x] Imposer la durée protocolaire de 10 ms sans permettre à une frame Opus valide mais de taille inattendue de tuer un worker.
- [x] Protéger tous les fallbacks de décodage et produire du silence borné en cas d'erreur.
- [x] Garantir qu'un encodeur Opus stateful n'est utilisé que par un seul flot d'exécution à la fois.
- [x] Compter les erreurs encode/decode, tailles inattendues et usages PLC.
- [x] Tester les durées Opus 2,5/5/10/20 ms, PLC, payloads invalides et retours natifs trop courts/longs.

Critère de sortie : aucun paquet reçu ne peut terminer le mixeur ou un callback, et les anomalies apparaissent dans les statistiques.

### P0.2 Sessions UDP et contrôle — Terminé

- [x] Refuser tout paquet UDP sans session TCP active correspondante.
- [x] Ne jamais créer un client ou remplacer son adresse à partir d'un paquet UDP anonyme.
- [x] Vérifier l'adresse source côté client et serveur.
- [x] Empêcher une ancienne session TCP de marquer une nouvelle session comme déconnectée.
- [x] Borner les buffers de framing JSON TCP et imposer un hello initial.
- [x] Déployer le protocole V2 strict avec token de session renouvelé, direction explicite, stream réservé et epoch interne de routage.

Critère de sortie : le test de création de clients fantômes et le détournement d'adresse échouent sans générer de trafic retour.

### P0.3 Routage et mix-minus — Terminé

- [x] Routage fail-closed lorsque le contrôle est absent ou que le masque de route de la frame est absent ou invalide.
- [x] Calculer le mix-minus dans le domaine linéaire, puis limiter séparément chaque destination.
- [x] Vérifier Plateau, VMix, Régie et `feed_to_regie` par tests purs.
- [x] Rendre explicite l'état « aucune route ».

Critère de sortie : une perte TCP coupe l'émission dès sa détection ou à l'expiration du liveness, et le mix-minus conserve correctement les autres sources à plein niveau.

### P0.4 Lifecycle et reconnexion — Terminé (validation sans matériel)

- [x] Suivre et joindre tous les workers audio/réseau de longue durée.
- [x] Rendre le lifecycle déterministe : démarrage serveur idempotent, redémarrage d'une instance arrêtée refusé explicitement et arrêts répétés sûrs.
- [x] Remplacer les sleeps de reconnexion par des attentes interruptibles.
- [x] Empêcher deux générations de contrôle de partager ou fermer le même socket.
- [x] Vider l'ancien backlog UDP avant une nouvelle session audio.
- [x] Exposer la mort inattendue d'un worker dans l'état de santé.

Critère de sortie : 100 générations réseau simulées ne laissent aucun worker supplémentaire et ne rejouent aucun ancien paquet.

### P0.5 Périphériques et callbacks — Terminé (validation sans matériel)

- [x] Ne jamais appeler `sounddevice._terminate/_initialize` pendant un live.
- [x] Supprimer tous les `QThread.terminate()`.
- [x] Fermer le Return input et vider son ring lorsqu'il est désactivé.
- [x] Rendre les changements de périphérique transactionnels, avec rollback si le nouveau stream ne démarre pas.
- [x] Valider séparément la compatibilité entrée et sortie et restaurer un endpoint par identité, index et direction.
- [x] Pour un casque Bluetooth, ouvrir l'entrée avant la sortie afin de stabiliser le profil mains-libres avant le playback.
- [x] Sortir codec, jitter buffer, sockets et files extensibles des callbacks PortAudio.
- [x] Dropper les données les plus anciennes lorsqu'une file temps réel est pleine.

Critère de sortie : refresh, disable Return et arrêt/redémarrage ne laissent aucun stream fantôme et ne bloquent pas la GUI.

### P0.6 UI fail-safe — Terminé

- [x] Faire transiter les callbacks hotkeys vers Qt avec des signaux queued.
- [x] Empêcher un toggle de se redéclencher sur une touche étrangère.
- [x] Relâcher immédiatement les PTT lors d'une perte de focus, d'un arrêt, d'une déconnexion ou d'un état dégradé.
- [x] Distinguer visuellement contrôle TCP, transport UDP et santé audio.
- [x] Interdire les opérations dangereuses pendant le live.

Critère de sortie : l'état affiché correspond à la chaîne réellement opérationnelle et aucun raccourci ne manipule directement un widget hors thread Qt.

## 5. P1 — architecture professionnelle

### P1.1 Modèle fixe mais extensible — Terminé pour les trois bus fixes

- [x] Introduire un catalogue central immuable `BusDefinition` pour les trois bus.
- [x] Générer labels, couleurs, listes et validations depuis ce catalogue.
- [x] Isoler un moteur de routage pur `Source → Bus → Output/Monitor`.
- [x] Centraliser les choix spécifiques à Régie/Plateau/VMix dans le catalogue et le moteur de routage.
- [x] Conserver les usages PTT/écoute et les trois bus actuels.

### P1.2 Profil et session — Terminé

- [x] Séparer `ClientProfile` persistant de `ClientState` live.
- [x] Ne pas créer de codecs, jitter buffers ou compteurs pour les profils hors ligne.
- [x] Définir une identité canonique UUID → client ID et refuser explicitement les collisions CRC32.
- [x] Nettoyer les profils historiques sans supprimer automatiquement des données utilisateur.

### P1.3 Protocole V2 — Terminé

- [x] Versionner explicitement hello, contrôle et paquet audio.
- [x] Ajouter token de session 64 bits, direction, identifiant de stream réservé, masque de bus et epoch interne de routage.
- [x] Valider tailles, types, identité, formats audio supportés et limites avant mutation d'état.
- [x] Refuser clairement les paquets et handshakes des anciennes versions ; aucun runtime V1 n'est conservé.

### P1.4 Moteur temps réel — En cours

- [x] Mettre en place des ring buffers SPSC préalloués autour des callbacks.
- [x] Confier encodeurs, décodeurs et sockets à des workers propriétaires.
- [ ] Introduire une estimation de jitter et une politique adaptative bornée.
- [ ] Mesurer et corriger la dérive lente entre horloges audio.
- [ ] Étudier un adaptateur périphérique 8/16/44,1 kHz ↔ moteur 48 kHz, hors callbacks, avant d'autoriser les endpoints WDM-KS natifs.
- [x] Formaliser la politique drop-oldest/resync de chaque file.

### P1.5 Persistance et configuration — Terminé

- [x] Créer un repository de presets à écrivain unique avec debounce.
- [x] Ajouter schéma, version, migrations, backup et distinction absent/corrompu/I/O.
- [x] Séparer snapshot sous verrou et I/O hors verrou.
- [x] Faire valider l'aperçu GUI serveur par la même migration de schéma que le moteur et les écritures différées.

### P1.6 Observabilité — En cours

- [x] Logs tournants dans `%LOCALAPPDATA%/py-intercom/logs`.
- [x] Santé par worker, stream et session.
- [x] Métriques : paquets manquants ou tardifs, PLC, profondeur JB/files, erreurs encode/decode, under/overflows, âge du dernier UDP et TCP.
- [ ] Compter explicitement les paquets réordonnés et dupliqués.
- [ ] Export de diagnostic opérateur en un clic.
- [ ] Aucune exception inattendue avalée sans log ni compteur.

### P1.7 UI/UX opérateur — En cours

- [x] Formaliser les états `Déconnecté`, `Connexion`, `En ligne`, `Dégradé`, `Reconnexion`.
- [x] Structurer clairement les zones de préparation et d'exploitation live dans les deux applications.
- [x] Unifier client et serveur avec un design system Qt commun, lisible et accessible.
- [x] Organiser la régie en deux colonnes redimensionnables : supervision à gauche, routage audio à droite.
- [x] Remplacer les grands tableaux vides par des états d'attente explicites côté régie.
- [ ] Étudier une séparation en écrans distincts si les essais opérateur montrent que la hiérarchie actuelle ne suffit pas.
- [ ] Ajouter un preflight micro/casque/Return/raccourcis/réseau.
- [x] Verrouiller clairement les réglages dangereux pendant le live et rendre les changements audio transactionnels.
- [x] Distinguer les endpoints homonymes par nom, API hôte, index et fréquence native ; mémoriser le filtre « tous les périphériques ».
- [x] Ne sélectionner aucun micro/casque implicitement lorsque la restauration du preset est ambiguë ou impossible.
- [x] Séparer le rafraîchissement VU rapide des rafraîchissements structurels plus lents.
- [ ] Revue visuelle sur toutes les résolutions et facteurs DPI de production (validation offscreen acquise à 1060×800 client, 1040×720 et 1280×820 serveur).

### P1.8 Industrialisation — En cours

- [x] Ajouter `pyproject.toml`, version applicative et dépendances directes explicites.
- [x] Verrouiller PyInstaller et corriger le fallback `python` de `build_exe.ps1`.
- [x] Faire exécuter tests et compilation par le workflow avant toute release.
- [ ] Ajouter tests d'intégration client/serveur locaux sans matériel.
- [x] Documenter version, empreinte et licences tierces dans `THIRD_PARTY_NOTICES.md`.
- [ ] Retrouver et figer la provenance amont reproductible exacte de `bin/opus.dll`.

## 6. Validation audio et réseau

### Tests automatisés obligatoires

- packets valides, tronqués, surdimensionnés et fuzz ciblé ;
- wrap, perte, reorder, duplicata et burst UDP ;
- frames Opus de plusieurs durées ;
- mix-minus et routes des trois bus ;
- remplacement de session et spoof d'adresse ;
- arrêt/reconnexion répétés ;
- écritures de preset concurrentes ;
- logique hotkeys sans GUI réelle.
- profils Bluetooth A2DP/HFP homonymes, compatibilité directionnelle et renumérotation PortAudio.

### Banc matériel avant qualification production

- Ethernet et Wi-Fi représentatifs ;
- cartes son et VB-Cable réellement utilisés ;
- 2, 5 puis 10 clients ;
- mesure loopback de latence P50/P95/P99 ;
- endurance minimale de quatre heures ;
- hotplug, perte réseau, redémarrage serveur et reconnexion ;
- comptage automatique des discontinuités, xruns, PLC et resync.

La qualification production reste `Non acquise` tant que ce banc n'a pas été exécuté.

## 7. Éléments terminés

### Nettoyage produit — Terminé le 6 août 2026

- [x] Suppression du client web et de son bridge.
- [x] Suppression Docker, workflow GHCR et dépendances web.
- [x] GUI obligatoire pour client et serveur.
- [x] Suppression des anciennes boucles headless et arguments associés.
- [x] Documentation README/playbook réalignée.
- [x] Tests des points d'entrée GUI.

Validation : compilation Python, imports GUI, `pip check` et recherche de résidus réussis ; la validation globale courante figure dans le journal.

### Sûreté temps réel P0 — Terminée le 6 août 2026

- [x] Protocole audio/contrôle V2 strict, UDP lié à un token de session et remplacement de session sans fuite d'ancien audio.
- [x] Routage fixe pur, fail-closed, mix-minus linéaire et limitation par destination.
- [x] Codecs, réseau et jitter buffers sortis des callbacks audio ; rings bornés à politique drop-oldest.
- [x] Workers possédés et joints, reconnexion par génération et 100 cycles automatisés sans fuite de thread.
- [x] Changements de périphérique transactionnels et erreurs de streams visibles.

### Fondations professionnelles P1 — Livrées le 6 août 2026

- [x] Profils persistants séparés des sessions et identité canonique validée.
- [x] Presets schématisés/versionnés, migrations en mémoire, backup atomique et écrivain unique différé.
- [x] FSM opérateur et PTT fail-safe ; santé contrôle/UDP/audio visible dans les deux GUI.
- [x] Logs tournants, crash reports, packaging `src`, workflow Windows et notices de redistribution.
- [x] Exécutables GUI client/serveur et wheel Python construits avec succès.

### Sélection audio Windows/Bluetooth — Terminée le 7 août 2026 (validation logicielle)

- [x] Restauration directionnelle empêchant un endpoint de sortie homonyme de remplacer le microphone sauvegardé.
- [x] Validation 48 kHz indépendante pour l'entrée et la sortie.
- [x] Libellés non ambigus et persistance/activation automatique de l'inventaire complet pour les presets non-WASAPI.
- [x] Démarrage client entrée puis sortie, cleanup transactionnel et diagnostic Bluetooth exploitable.
- [x] Tests A2DP/HFP, index recyclé et changement de numérotation.

Validation matérielle complète encore requise : connexion réelle, hotplug et endurance avec les casques de production.

### Modernisation UI/UX desktop — Terminée le 10 août 2026 (première passe)

- [x] Palette, typographie, espacements, champs, boutons, cartes et tableaux unifiés dans le thème commun.
- [x] Client réorganisé en statut, connexion, périphériques puis exploitation écoute/PTT.
- [x] Régie réorganisée en statut/actions, configuration/Return puis supervision et routage.
- [x] Espace régie en deux colonnes avec séparateur horizontal ajustable et tables audio agrandies.
- [x] États vides explicites et descriptions accessibles ajoutés aux tableaux opérateur.
- [x] Captures client et serveur contrôlées visuellement sans matériel audio.

La validation avec les écrans tactiles, facteurs DPI et résolutions réellement utilisés en production reste inscrite à P1.7.

## 8. Journal des changements

### 6 août 2026 — Initialisation V2

- Création de cette roadmap à partir de l'audit architecture, temps réel et UI/UX.
- Enregistrement des décisions produit desktop/GUI/trois bus fixes.
- Démarrage des lots P0 serveur, client et périphériques/UI.

### 6 août 2026 — Stabilisation P0

- Frames Opus inattendues neutralisées sans arrêt du mixeur ou des callbacks.
- Routage passé en fail-closed et mix-minus corrigé avant limitation par destination.
- UDP lié à une session de contrôle et à son endpoint ; clients inconnus et sources incorrectes rejetés.
- Lifecycle réseau client isolé par génération, reconnexion interruptible et backlog UDP purgé.
- Workers serveur suivis, joints et exposés dans l'état de santé.
- Refresh PortAudio non destructif en live, suppression de `QThread.terminate()` et hotkeys Qt thread-safe.
- Validation intermédiaire : 86 tests automatisés réussis avant ouverture des lots P1.

### 6 août 2026 — Fondations P1

- Catalogue central des trois bus fixes intégré aux moteurs et aux GUI.
- Parser TCP commun borné, handshake ordonné et versions strictement validées.
- `ClientProfile` séparé des sessions live ; suppression des codecs/JB fantômes au chargement.
- Presets sauvegardés par écrivain unique différé avec flush, backup et erreurs explicites.
- Logs tournants persistants ajoutés pour les exécutables sans console.
- Packaging `src` et version `2.0.0.dev0` ajoutés ; bootstrap Windows/Opus factorisé.
- Workflow de release protégé par compilation et tests ; versions pytest/PyInstaller verrouillées.
- Paquet audio V2 et frame ring préalloué développés en modules communs, intégration moteur en cours.

### 6 août 2026 — Intégration P0/P1 et qualification automatisée

- Protocole V1 retiré du code public et des moteurs ; header V2 de 32 octets validé strictement dans les deux directions.
- Token renouvelé à chaque session, endpoint UDP lié au contrôle, identité UUID/CRC32 validée et collisions refusées.
- Epoch de routage serveur ajouté : une reconnexion purge les mixes et sorties physiques encore associés à l'ancienne session.
- Commit de session serveur rendu atomique (socket, token, codecs et epoch) ; changement de décodeur et envoi UDP protégés contre les courses de remplacement.
- `forget_client()` révoque désormais la session active et purge tous les buffers dans la même transaction.
- Types du hello et des états de contrôle validés avant mutation ; booléens ambigus, gains non finis et bus inconnus provoquent un refus fail-closed.
- Routage pur trois bus, mix-minus, Return et monitoring Régie intégrés aux moteurs.
- Rings préalloués intégrés aux entrées/sorties client et serveur ; encodeurs/décodeurs possédés par leurs workers.
- FSM client, relâchement PTT fail-safe, santé détaillée et cadence UI différenciée intégrés.
- Presets client/serveur passés au schéma V2 avec migration, backup et refus des versions futures ; les clés inconnues sont conservées côté client.
- Aperçu et modifications hors-live de la GUI serveur alignés sur le schéma canonique, sans réécriture automatique d'un preset incompatible.
- Validation : **327 tests réussis en 4,46 s**, `compileall` réussi et `pip check` sans dépendance cassée.
- Stabilité concurrence : sous-suite temps réel client/serveur de 51 tests passée cinq fois consécutivement.
- Packaging : `client.exe` et `server.exe` PyInstaller construits et smoke-testés (code retour 0) ; wheel `py_intercom-2.0.0.dev0` construit.
- CI Windows exécutée aussi sur les pull requests ; publication GitHub désactivée par défaut et soumise à la variable `PY_INTERCOM_ENABLE_PUBLIC_RELEASE` tant que la provenance d'Opus bloque la redistribution.
- Artefacts locaux `build/` et `dist/` exclus de Git ; les exécutables sont distribués par les artefacts du workflow.

### 7 août 2026 — Sélection des périphériques Bluetooth

- Reproduction sur les endpoints réels d'un casque G435 : les côtés HFP entrée/sortie partageaient le même nom et la même API, ce qui restaurait le mauvais index.
- Résolution rendue directionnelle, index sauvegardé vérifié contre l'identité et correspondances ambiguës laissées volontairement sans sélection.
- Compatibilité 48 kHz enregistrée séparément par direction ; endpoints WDM-KS natifs incompatibles filtrés sans masquer les variantes Windows partagées utilisables.
- Menus enrichis avec API/index/fréquence, option « tous les périphériques » persistée et réactivée automatiquement pour un preset non-WASAPI.
- Ordre de démarrage client inversé pour établir le profil micro HFP avant la sortie et erreurs de démarrage traduites avec un diagnostic actionnable.
- Documentation d'exploitation Bluetooth et tests de non-régression ajoutés.
- Validation globale : **346 tests réussis en 4,51 s**, `compileall`, `git diff --check` et `pip check` réussis. L'essai duplex réel casque connecté reste inscrit au banc matériel.

### 10 août 2026 — Modernisation UI/UX desktop

- Nouveau design system Qt sombre partagé : hiérarchie typographique, cartes, champs empilés, boutons sémantiques, états et tableaux simplifiés.
- Client recentré sur le flux opérateur « préparer, connecter, parler » avec les commandes d'écoute, gains, VU et PTT visibles dans une même zone live.
- Régie recentrée sur le démarrage, la supervision de l'équipe et le routage ; les tables sans ligne expliquent désormais l'action ou l'événement attendu.
- Disposition serveur affinée en deux colonnes redimensionnables afin de conserver des tables bus/sorties lisibles sans maximiser la fenêtre.
- Libellés associés à leurs contrôles pour la navigation clavier et tailles minimales relevées afin d'éviter les chevauchements.
- Captures offscreen vérifiées à 1060×800 et 1220×900 ; tests dédiés au design system ajoutés.
- Validation globale : **350 tests réussis en 4,72 s**, `compileall` et `git diff --check` réussis.
- `client.exe` et `server.exe` reconstruits avec PyInstaller 6.19.0 puis smoke-testés via `--help` (codes retour 0).

### Prochain lot prioritaire

1. banc matériel et réseau : latence P50/P95/P99, dérive, xruns et endurance quatre heures ;
2. jitter buffer adaptatif borné et correction douce de dérive d'horloge ;
3. préflight opérateur et export de diagnostic en un clic ;
4. test d'intégration client/serveur complet sans périphérique physique ;
5. remplacement de `opus.dll` par un artefact à provenance amont reproductible.
