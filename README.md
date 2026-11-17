# Clip

Crée automatiquement des clips verticaux (9:16, sous-titrés en français) à partir de lives et vidéos Twitch / YouTube, puis les publie sur **YouTube Shorts** et **TikTok**. Tout tourne en local sur ton serveur : aucune API d'IA payante.

## Comment ça marche

1. **Surveillance** des chaînes ajoutées (toutes les 10 min) : les lives sont enregistrés, les nouvelles vidéos/rediffusions téléchargées (`yt-dlp`). Tu peux aussi coller un lien à la main.
2. **Transcription** locale avec Whisper (`faster-whisper`, CPU), mot par mot.
3. **Détection des moments forts** : pics de son, densité de parole, mots d'excitation (« mdr », « incroyable », « !»…), puis notation et titre par une IA locale (Ollama, `qwen2.5:3b`).
4. **Montage** : clip d'environ 1 min, prolongé jusqu'à la fin de la phrase (3 min max). Au-delà de 90 s, découpage en « Partie 1/2 », « Partie 2/2 ». Format 1080x1920, fond flouté, sous-titres dynamiques (mot prononcé en jaune).
5. **Publication automatique**, espacée (90 min mini, 6 par jour et par plateforme). Les parties sont publiées dans l'ordre.

Une interface web permet de gérer les chaînes, voir les clips, changer un titre, supprimer ou republier.

## Installation sur le serveur

Prévu pour un PC sous Linux (testé pour un Ryzen 7 4700U / 16 Go). Il faut Docker :

```bash
curl -fsSL https://get.docker.com | sh
git clone https://github.com/theocyx/clip.git && cd clip
cp .env.example .env
nano .env            # au minimum : ADMIN_PASSWORD, PUBLIC_URL, DOMAIN
docker compose --profile https up -d --build
```

L'interface est sur `https://<ton domaine>` (identifiant `admin` + ton mot de passe).

**Double authentification (MFA)** : à la première connexion, Clip affiche un QR code à scanner avec Google Authenticator (ou Authy, 2FAS…). Ensuite il faut identifiant + mot de passe + code à 6 chiffres. La connexion reste valable 30 jours (`SESSION_DAYS`). Téléphone perdu : `docker compose exec web python -m app.web.mfa reset`, puis reconnecte-toi pour scanner un nouveau QR code. Le premier traitement télécharge les modèles d'IA (≈ 2,5 Go).

### Nom de domaine et HTTPS

TikTok exige une adresse en **https**. Le plus simple et gratuit : un sous-domaine [DuckDNS](https://www.duckdns.org) qui pointe vers ta box, avec les ports 80 et 443 redirigés vers le PC. Caddy (inclus) obtient le certificat tout seul. Sans HTTPS, lance juste `docker compose up -d` et ouvre `http://IP-du-PC:8000` après avoir mis `WEB_BIND=0.0.0.0` dans `.env`.

### Accélération par le GPU intégré

La Radeon Vega du Ryzen peut encoder les vidéos : dans `docker-compose.yml` décommente `devices: ["/dev/dri:/dev/dri"]` (service `worker`) et mets `VIDEO_CODEC=h264_vaapi` dans `.env`.

### Laptop utilisé comme serveur

- Empêcher la mise en veille capot fermé : `HandleLidSwitch=ignore` dans `/etc/systemd/logind.conf`, puis `sudo systemctl restart systemd-logind`.
- Les conteneurs redémarrent seuls après un reboot (`restart: unless-stopped`).

### Temps de traitement (estimation pour un 4700U)

Pour 1 h de vidéo : transcription ≈ 15-25 min (`WHISPER_MODEL=small`), IA ≈ 1-2 min, rendu ≈ 1 min par clip. Mets `WHISPER_MODEL=base` pour aller 2x plus vite (sous-titres un peu moins précis).

## Connecter YouTube

1. Sur [console.cloud.google.com](https://console.cloud.google.com) : crée un projet, active **YouTube Data API v3**.
2. « Écran de consentement OAuth » : type *Externe*, ajoute ton adresse comme utilisateur test.
3. « Identifiants » → *ID client OAuth* → *Application Web*. URI de redirection : `https://<ton domaine>/auth/youtube/callback`.
4. Copie l'ID et le secret dans `.env` (`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`), redémarre (`docker compose up -d`), puis **Comptes → Connecter ma chaîne YouTube**.

⚠️ Tant que le projet Google n'a pas passé l'[audit de l'API](https://support.google.com/youtube/contact/yt_api_form), Google force les vidéos envoyées en **privé**. Le quota gratuit permet environ 6 envois par jour.

## Connecter TikTok

1. Sur [developers.tiktok.com](https://developers.tiktok.com) : crée une app, ajoute les produits **Login Kit** et **Content Posting API** (avec *Direct Post*), scopes `user.info.basic`, `video.upload`, `video.publish`.
2. URI de redirection : `https://<ton domaine>/auth/tiktok/callback` (le domaine doit être vérifié).
3. Copie la *Client key* et le *Client secret* dans `.env`, redémarre, puis **Comptes → Connecter mon compte TikTok**.

⚠️ Tant que TikTok n'a pas **audité l'app** (quelques jours à quelques semaines), les vidéos ne peuvent être publiées qu'en **privé** (`TIKTOK_PRIVACY=SELF_ONLY`). Après approbation, mets `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`. Alternative : `TIKTOK_MODE=draft` envoie les clips en brouillon dans l'appli TikTok, tu n'as plus qu'à appuyer sur « Publier ».

## Apprendre de ce qui marche (page **Stats**)

Toutes les 6 h, Clip relève les vues, likes, commentaires (et la rétention YouTube) de chaque clip publié, puis compare les clips entre eux à 3 jours d'âge. Dès 10 clips mesurés, il ajuste tout seul, doucement :

- le poids des signaux qui repèrent les moments forts (pics de son, débit de parole, mots d'excitation) ;
- la durée visée des clips (avec ±15 % de variation pour continuer à tester) ;
- la confiance accordée à la note de l'IA ;
- les titres et accroches : l'IA locale compare les meilleurs et les pires clips, en tire des règles, et s'en sert pour les clips suivants.

Tout reste local. La page **Stats** montre les chiffres, ce qui a été appris et un bouton pour lancer l'analyse.

- **Rétention YouTube** : active aussi « YouTube Analytics API » dans la console Google, puis reconnecte YouTube.
- **Vues TikTok** : ajoute le scope `video.list` à l'app TikTok, mets `TIKTOK_STATS=true`, puis reconnecte TikTok.
- Les clips privés (projet Google ou app TikTok pas encore audités) n'ont pas de vues : rien ne sera appris avant.

## Réglages utiles (`.env`)

| Variable | Rôle | Défaut |
|---|---|---|
| `CLIPS_PER_VIDEO` | clips par heure de vidéo | 3 |
| `CLIP_TARGET` / `CLIP_MAX` | durée visée / max (s) | 60 / 180 |
| `PART_MAX` | au-delà, découpage en parties | 90 |
| `AUTO_PUBLISH` | publier sans validation | true |
| `MIN_MINUTES_BETWEEN_POSTS` / `MAX_POSTS_PER_DAY` | rythme de publication | 90 / 6 |
| `HASHTAGS` | hashtags ajoutés | `#shorts #clip #fr` |
| `HOOK_TITLE` / `HOOK_SECONDS` | accroche en haut de la vidéo / durée (0 = tout le clip) | true / 0 |
| `KEEP_SOURCE_VIDEOS` | garder les vidéos complètes | false |

## Droits

Clipper le contenu d'autres créateurs sans leur accord peut entraîner des retraits ou des avertissements (strikes). Chaque publication crédite la vidéo d'origine dans la description.

## Développement

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Structure : `app/pipeline` (téléchargement, transcription, détection, sous-titres, rendu), `app/publish` (YouTube, TikTok), `app/worker.py` (tâches de fond), `app/web` (interface).
