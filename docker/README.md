# py-intercom — Docker web server

Lance le serveur web py-intercom (client web) dans un conteneur Docker.  
Le repo est cloné depuis le Gitea local au moment du build.

## Prérequis

- Docker + Docker Compose v2
- Le Gitea local `10.0.0.4:3000` doit être accessible depuis la machine qui fait le build

## Démarrage rapide

```bash
cp .env.example .env
# Éditer .env si besoin
docker compose up -d --build
```

Le serveur sera accessible sur `https://<IP_MACHINE>:8443`  
(certificat auto-signé par défaut — le navigateur affichera un avertissement à accepter).

## Variables d'environnement

| Variable       | Défaut                                              | Description                              |
|----------------|-----------------------------------------------------|------------------------------------------|
| `GITEA_URL`    | `http://10.0.0.4:3000/lfpoulain/py-intercom.git`   | URL du repo Gitea                        |
| `GITEA_BRANCH` | `main`                                              | Branche à cloner                         |
| `WEB_PORT`     | `8443`                                              | Port exposé                              |
| `SSL_ADHOC`    | `1`                                                 | `1` = certificat auto-signé (werkzeug)   |
| `SSL_CERT`     | *(vide)*                                            | Chemin vers le certificat SSL custom     |
| `SSL_KEY`      | *(vide)*                                            | Chemin vers la clé privée SSL custom     |

## SSL avec certificats custom

Monter les fichiers dans le conteneur et renseigner les chemins :

```yaml
# dans docker-compose.yml, sous volumes:
volumes:
  - /chemin/local/cert.pem:/certs/cert.pem:ro
  - /chemin/local/key.pem:/certs/key.pem:ro
```

```env
SSL_ADHOC=0
SSL_CERT=/certs/cert.pem
SSL_KEY=/certs/key.pem
```

## Rebuild après un push

```bash
docker compose up -d --build --force-recreate
```
