# Docker - Web module

Ce dossier contient le déploiement Docker du module web (`src/py_intercom/web`).

## Fichiers

- `docker/docker-compose.yml`
- `docker/web/Dockerfile`
- `docker/web/requirements.txt`

## Lancer sur un serveur

Depuis la racine du repo :

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

Au démarrage, le conteneur :

1. clone le repo depuis Gitea si absent,
2. sinon fait un `git pull --ff-only`,
3. lance le module web.

Repo par défaut configuré :

`http://10.0.0.4:3000/lfpoulain/py-intercom.git`

Logs :

```bash
docker compose -f docker/docker-compose.yml logs -f py-intercom-web
```

Arrêt :

```bash
docker compose -f docker/docker-compose.yml down
```

## Accès

- URL : `https://<ip-du-serveur>:8443/`
- Certificat : `--ssl-adhoc` (auto-signé, prévu pour test/LAN)

## Notes réseau

- Le module web écoute en HTTP(S) sur `8443`.
- La découverte automatique LAN repose sur UDP broadcast sur `5002`.
- Le bridge web contacte le serveur intercom cible en UDP/TCP (audio/control) selon l'IP/port saisis dans l'UI.

## Variables utiles

- `GITEA_REPO_URL` : URL du repo à cloner/pull
- `APP_DIR` : répertoire de checkout dans le conteneur (défaut `/opt/py-intercom`)
- `WEB_PORT` : port interne de l'app web (défaut `8443`)

Le checkout Git est persisté dans le volume nommé `py_intercom_web_repo`.
