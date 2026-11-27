# Clip

Crée automatiquement des clips verticaux (9:16, sous-titrés en français) à partir de lives et vidéos Twitch / YouTube, puis les publie sur **YouTube Shorts** et **TikTok**. Tout tourne en local sur ton serveur : aucune API d'IA payante.

## Comment ça marche

1. **Surveillance** des chaînes ajoutées (toutes les 10 min) : les lives sont enregistrés, les nouvelles vidéos/rediffusions téléchargées (`yt-dlp`). Tu peux aussi coller un lien à la main.
2. **Transcription** locale avec Whisper (`faster-whisper`, CPU), mot par mot.
3. **Détection des moments forts** : pics de son, densité de parole, mots d'excitation (« mdr », « incroyable », « !»…), puis notation et titre par une IA locale (Ollama, `qwen2.5:3b`).
4. **Montage** : clip d'environ 1 min, prolongé jusqu'à la fin de la phrase (3 min max). Au-delà de 90 s, découpage en « Partie 1/2 », « Partie 2/2 ». Format 1080x1920 : si une facecam est détectée, le streamer en haut et le jeu en bas (format split), sinon fond flouté. Sous-titres dynamiques (mot prononcé en jaune).
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

La Radeon Vega du Ryzen encode les vidéos : `docker-compose.yml` donne `/dev/dri` au `worker` et `VIDEO_CODEC=auto` (défaut) l'utilise s'il est accessible, sinon le rendu reste sur le CPU. Sans GPU, retire la ligne `devices`.

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
5. Une fois connecté, repasse sur « Écran de consentement OAuth » et clique **Publier l'application** (mode *Production*). En mode *Test*, Google révoque la connexion au bout de **7 jours** : les envois s'arrêtent (Clip met alors la file en pause, prévient sur Discord et affiche « connexion expirée » dans Comptes ; il suffit de reconnecter). L'avertissement « appli non validée » à la connexion est normal : clique *Paramètres avancés → Accéder à l'application*.

⚠️ Tant que le projet Google n'a pas passé l'[audit de l'API](https://support.google.com/youtube/contact/yt_api_form), Google force les vidéos envoyées en **privé**. Le quota gratuit permet environ 6 envois par jour.

## Connecter TikTok

1. Sur [developers.tiktok.com](https://developers.tiktok.com) : crée une app, ajoute les produits **Login Kit** et **Content Posting API** (avec *Direct Post*), scopes `user.info.basic`, `video.upload`, `video.publish`.
2. URI de redirection : `https://<ton domaine>/auth/tiktok/callback` (le domaine doit être vérifié).
3. Copie la *Client key* et le *Client secret* dans `.env`, redémarre, puis **Comptes → Connecter mon compte TikTok**.

⚠️ Tant que TikTok n'a pas **audité l'app** (quelques jours à quelques semaines), les vidéos ne peuvent être publiées qu'en **privé** (`TIKTOK_PRIVACY=SELF_ONLY`). Après approbation, mets `TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE`. Alternative : `TIKTOK_MODE=draft` envoie les clips en brouillon dans l'appli TikTok, tu n'as plus qu'à appuyer sur « Publier ».

## Mettre une plateforme en pause

Sur le tableau de bord (et la page **Comptes**), un bouton par plateforme met la publication en pause ou la réactive. **TikTok est en pause par défaut** tant que l'app n'est pas validée. En pause, les clips restent en attente pour cette plateforme et partent à la réactivation (en mode GTA 6, les plus récents d'abord) ; « Vider la file » les retire (republiables depuis **Clips**).

## Quota du jour et envoi manuel (page **Clips**)

Les envois du jour vont aux clips **les mieux notés**. La limite par défaut est de 100 envois/jour (quota « Video Uploads per day » de YouTube) ; tu peux en fixer une par plateforme dans **Clips** (0 = aucune). Si YouTube refuse un envoi pour quota atteint, les envois reprennent tout seuls le lendemain (minuit heure du Pacifique) et tu es notifié. Le quota réel se voit dans la console Google (YouTube Data API v3 › Quotas) (en mode GTA 6, les plus récents comptent plus). La page **Clips** affiche les envois utilisés et prévus d'ici minuit, et trie les clips en onglets : **À poster à la main** (ceux qui ne passeront pas aujourd'hui), **Prévus aujourd'hui** (avec leur ordre), **Déjà publiés** (auto ou à la main). Les **heures d'envoi** et l'écart minimum entre deux envois se changent aussi ici (ou dans **Comptes**), sans toucher au `.env`. Pour chaque clip : **Télécharger** le MP4, **Textes à copier** (titre, description, légende TikTok, hashtags), puis **✅ Posté sur YouTube/TikTok** pour qu'il ne parte pas en double.

## Apprendre de ce qui marche (page **Stats**)

Toutes les 6 h, Clip relève les vues, likes, commentaires (et la rétention YouTube) de chaque clip publié, puis compare les clips entre eux à 3 jours d'âge. Dès 10 clips mesurés, il ajuste tout seul, doucement :

- le poids des signaux qui repèrent les moments forts (pics de son, débit de parole, mots d'excitation) ;
- la durée visée des clips (avec ±15 % de variation pour continuer à tester) ;
- la confiance accordée à la note de l'IA ;
- les titres et accroches : l'IA locale compare les meilleurs et les pires clips, en tire des règles, et s'en sert pour les clips suivants.

Tout reste local. La page **Stats** montre les chiffres, ce qui a été appris et un bouton pour lancer l'analyse.

**Amélioration continue du montage** (`EXPERIMENTS=true`) : chaque vidéo utilise la meilleure version connue de chaque réglage, et 25 % des vidéos (`EXPERIMENT_RATE`) essaient une autre version d'**un seul** réglage : voix et débit pour les actus et les histoires (`NEWS_VOICES`, `STORY_VOICES`), zoom sur la réaction, mots forts en couleur et sous-titres qui rebondissent pour les clips. Dès qu'une version fait nettement mieux sur au moins `EXPERIMENT_MIN` vidéos mesurées (4), elle devient la version utilisée ; le tableau est sur **Stats**. Les meilleures et les pires actus / histoires servent aussi d'exemples à l'IA qui écrit les suivantes (premières phrases, titres, accroches).

- **Rétention YouTube** : active aussi « YouTube Analytics API » dans la console Google, puis reconnecte YouTube.
- **Vues TikTok** : ajoute le scope `video.list` à l'app TikTok, mets `TIKTOK_STATS=true`, puis reconnecte TikTok.
- Les clips privés (projet Google ou app TikTok pas encore audités) n'ont pas de vues : rien ne sera appris avant.

## Faire grandir la chaîne (page **Stats**, carte « Croissance »)

- **Abonnés et monétisation** : à chaque relevé, Clip note les abonnés de la chaîne (courbe, rythme par jour, date estimée des 1000), les vues de nos Shorts sur 90 jours (objectif 10 M) et les heures de visionnage des vidéos longues (objectif 4000 h). YouTube Analytics donne aussi les **abonnés gagnés par vidéo** : ils comptent dans la note de chaque Short (on cherche ce qui fait s'abonner, pas seulement ce qui fait des vues).
- **On double ce qui marche** (`GROWTH_MIX=true`) : chaque format (clips GTA, actus, histoires) est comparé aux autres sur les `MIX_DAYS` derniers jours. Le format qui fait le plus de vues passe devant dans la file ; un format nettement en dessous n'est plus produit qu'une fois tous les `MIX_TEST_DAYS` jours (pour retester), un format au-dessus est produit plus souvent (jusqu'à `NEWS_MAX_PER_DAY` / `STORY_MAX_PER_DAY`).
- **Réserve** (`RESERVE=true`) : un clip GTA juste sous le seuil appris (note IA relevée, top %) n'est plus jeté ; il reste en file derrière les autres et part quand il n'y a rien de mieux, au lieu de laisser un créneau vide. `RESERVE_MAX` clips au plus.
- **Streamers qui marchent d'abord** (`SOURCE_PRIORITY=true`) : les lives et morceaux des streamers dont nos clips font le plus de vues sont enregistrés et traités en premier.
- **Lien d'abonnement** (`SUBSCRIBE_LINK=true`) : « 🔔 Abonne-toi… » avec le lien `?sub_confirmation=1` dans chaque description YouTube.
- **« Abonne-toi » à l'écran** : testé par l'amélioration continue sur une part des clips (`CLIP_CTA` = version par défaut) ; gardé seulement s'il fait mieux.
- **Interactions** (0 commentaire sur les 189 premiers Shorts) : la chaîne pose la question du clip en **1er commentaire** quelques minutes après l'envoi (`FIRST_COMMENT=true` ; demande l'autorisation « gérer les commentaires » : reconnecte YouTube dans **Comptes** une fois). Les questions sont des choix qu'on tranche en un mot (« Il aurait dû fuir ou se rendre ? »). Ligne « 📤 Envoie-le à un pote… » dans les descriptions (`SHARE_LINE`). La **question à l'écran** en fin de clip et l'**appel à l'écran** (« Abonne-toi » ou « Envoie ça à un pote ») sont essayés sur une part des clips et jugés sur le taux de likes / commentaires / partages (un commentaire compte double, un partage triple). Les clips retirés juste avant l'arrivée de la réserve sont remis en file une fois pour être triés à nouveau.
- **Réponses et playlists** (même autorisation) : toutes les 15 min (`COMMUNITY_EVERY`), la chaîne répond aux commentaires récents des spectateurs, et à ceux qui répondent à sa question, avec une phrase de l'IA locale qui relance la discussion (`COMMENT_REPLIES`, `REPLY_PER_RUN`=4, `REPLY_PER_DAY`=40 ; liens, insultes et spam ignorés, jamais d'auto-promo). Chaque vidéo est rangée dans une playlist publique par streamer (« Squeezie GTA 6 : les meilleurs moments »), actus, histoires et compilations (`PLAYLISTS`).
- **Titres « citation »** : la moitié des nouveaux clips (`TITLE_TEST_SHARE`) prend un titre façon @cyprienlefute, « Streamer : « phrase culte dite dans le clip » 😭 », l'autre garde « Streamer fait X GTA 6 ». Dès que chaque style a `EXPERIMENT_MIN` Shorts mesurés, celui qui fait le plus de vues devient le style par défaut (`CLIP_TITLE_STYLE`).
- **Compilations longues** (`COMPIL=true`) : chaque dimanche à partir de 18 h (`COMPIL_DAY`, `COMPIL_HOUR`), les `COMPIL_MAX` Shorts les plus vus de la semaine sont remontés en une vidéo 16:9 de 5 à 15 min (au-delà de 8 min, YouTube y place des pubs au milieu une fois la chaîne monétisée) (Short au centre sur fond flouté, numéro et titre sur les côtés, du moins bon au meilleur), avec chapitres, crédits des streamers et miniature, et publiés comme une vidéo normale. Ce sont les seules vidéos qui comptent pour les 4000 heures. Bouton « Monter la compilation maintenant » sur **Stats**. La miniature personnalisée demande une chaîne validée par téléphone (youtube.com/verify).
- **Estimation de la monétisation** : la carte affiche, au rythme réel (abonnés et vues des 7 derniers jours, heures des compilations), la date estimée d'éligibilité au Programme Partenaire (1000 abonnés et 10 M vues Shorts sur 90 jours ou 4000 h) et les revenus estimés par mois, maintenant et au seuil (`RPM_SHORTS`, `RPM_LONG` en € pour 1000 vues). C'est un ordre de grandeur : YouTube peut refuser une chaîne de clips d'autres streamers pour « contenu réutilisé ».

## Notifications (Discord ou Telegram)

- **Discord** : sur ton serveur, *Paramètres du salon → Intégrations → Webhooks → Nouveau webhook*, copie l'URL dans `DISCORD_WEBHOOK_URL`.
- **Telegram** : parle à **@BotFather**, `/newbot`, copie le jeton dans `TELEGRAM_BOT_TOKEN`. Envoie un message à ton bot, puis ouvre `https://api.telegram.org/bot<jeton>/getUpdates` : le nombre après `"chat":{"id":` va dans `TELEGRAM_CHAT_ID`.

Puis `docker compose up -d` et, dans **Comptes**, clique sur « Envoyer un message de test ».

## Horaires, crédit du streamer, sauvegardes

- Les clips ne sont publiés que pendant `PUBLISH_HOURS` (par défaut 12 h-14 h et 18 h-22 h, heure de Paris), espacés de `MIN_MINUTES_BETWEEN_POSTS`.
- La description cite le streamer, le lien de sa chaîne et un hashtag à son nom (`CREDIT_STREAMER=false` pour l'enlever).
- Chaque jour, la base est copiée dans `data/backups/` (les 7 dernières sont gardées). Pour restaurer : `docker compose down`, copie la sauvegarde sur `data/clip.db`, supprime `data/clip.db-wal` et `data/clip.db-shm`, puis `docker compose up -d`.
- Les vidéos sources sont effacées après 3 jours et les fichiers des clips déjà publiés après 14 jours.

## Passages sponsorisés exclus

Aucun clip n'est pris dans une pub, un placement de produit ou un code promo :
- vidéos YouTube : passages signalés sur SponsorBlock (sponsor, autopromo, « abonnez-vous », intro/outro) ;
- transcription : « sponsorisé », « partenariat », « code promo », « lien en description », « merci à … pour avoir sponsorisé »… (le passage autour est exclu : 20 s avant, 60 s après) ;
- l'IA locale signale aussi les extraits qui ressemblent à une pub ;
- texte à l'écran (« sponsorisé », « partenariat », « publicité »…) lu par OCR local sur une image toutes les 4 s.

Au démarrage, les clips déjà rendus mais pas encore publiés qui tombent dans ces passages sont retirés de la file (« annulé », motif « Retiré : … »). Réglages : `EXCLUDE_SPONSORED`, `SPONSORBLOCK`, `SPONSOR_OCR`.

## Hashtags tendance et anti-doublons

- Hashtags ajoutés selon le titre du live et ce qui est dit dans le clip : `#GTA6`, `#GTARP`, `#GTA5`, et le serveur RP reconnu (`#FlashBack`, `#21JumpClick`, `#FailyV`, `#NoPixel`). Autres serveurs : `RP_SERVERS=Lunar City=#LunarCity` dans `.env`.
- Quand plusieurs streamers vivent la même scène RP, un seul clip est gardé : les répliques entendues sont comparées avec les clips des 48 dernières heures (et l'heure du live). Le meilleur clip reste, l'autre n'est pas monté ou retiré de la file (« annulé », motif « Doublon »).

## Mode « GTA 6 day one »

Actif automatiquement de la veille au soir du 19/11/2026 pendant 3 jours (ou bouton sur le tableau de bord, ou `GTA6_MODE=on/off`). Pendant le mode :
- les lives sont vérifiés toutes les 90 s (Twitch en une seule requête, avec la catégorie « Grand Theft Auto VI » et le nombre de spectateurs) ;
- les lives GTA 6 sont enregistrés en priorité, les plus regardés d'abord (puis l'ordre du catalogue ou `GTA6_PRIORITY`), au plus `GTA6_MAX_RECORDINGS` à la fois ; les autres attendent en file ;
- chaque live est découpé en morceaux de 20 min traités pendant que le streamer joue encore, les morceaux GTA 6 passent devant le reste ;
- les clips GTA 6 les plus récents sont publiés d'abord, toutes les 30 min et à toute heure (le quota YouTube reste ~6 envois/jour) ;
- une notification est envoyée à chaque nouveau live GTA 6.

## Santé du serveur

Le tableau de bord affiche le disque, la température du CPU, la mémoire, la charge, l'état du worker et de l'IA. Une notification (Discord/Telegram) part quand le disque dépasse 90 % (ou moins de 5 Go libres), le CPU 90 °C, la mémoire 92 %, quand l'interface web, Ollama ou une tâche du worker ne répond plus, quand le worker ne donne plus signe de vie depuis 10 min, ou après un plantage (redémarrage brutal). Rappel toutes les 6 h tant que ça dure, puis « ✅ Réglé ». Réglages `HEALTH_*` (`HEALTH=false` pour couper).

## Découverte automatique des streamers

Toutes les 30 min (10 min pendant le mode GTA 6), Clip cherche les lives français sur Twitch (catégories `DISCOVER_GAMES`, par défaut GTA VI et GTA V) et les lives GTA FR sur YouTube, et ajoute ceux qui ont au moins `DISCOVER_MIN_VIEWERS` (150) spectateurs : filtre de titre GTA, lives seulement (pas de rediffusions), `DISCOVER_PER_RUN` (2) par recherche et `DISCOVER_MAX_SOURCES` (10) au total pour ménager le CPU. Ils sont marqués « auto » dans Sources ; une chaîne qui ne fait plus de GTA depuis `DISCOVER_EXPIRE_DAYS` (7) jours est retirée, et une chaîne que tu supprimes n'est jamais rajoutée. Bouton « Chercher maintenant » et activation/désactivation dans Sources.

## Vidéos d'actu générées automatiquement

En plus des clips, Clip fabrique seul des vidéos d'actu au format des comptes TikTok d'actu : `NEWS_PER_DAY` (3) par jour, entre `NEWS_HOURS` (7-22 h).

1. **Sujet** : lecture des flux RSS de `NEWS_FEEDS` (actu générale par défaut : franceinfo, Le Monde, 20 Minutes, Google Actualités), sujets de moins de 24 h, les plus repris par plusieurs médias en premier. Bons plans / promos écartés (`NEWS_EXCLUDE`), jamais deux fois le même sujet.
2. **Script** : l'IA locale réécrit les faits de l'article en 6 à 10 phrases courtes (~45 s) : accroche choc, faits, question finale. Un script qui contient un chiffre absent de l'article est refusé. Sujet noté sous `NEWS_MIN_SCORE` (6/10) : écarté.
3. **Voix off** : Piper, en local (voix `NEWS_VOICE`, téléchargée au premier usage, débit `NEWS_VOICE_SPEED`).
4. **Images** : un plan par phrase (~3-4 s) avec mouvement de caméra. Vidéos/photos libres de droits Pexels si `PEXELS_API_KEY`, ou Pixabay si `PIXABAY_API_KEY` (clé gratuite sur pixabay.com/api/docs), puis, sans clé, Openverse (`NEWS_OPENVERSE`, CC0 / domaine public / CC BY / CC BY-SA) et Wikimedia Commons (licences libres ; auteur crédité dans la description), sinon carte générée avec les mots forts. Aucune image d'article n'est reprise.
5. **Habillage animé** (`NEWS_MOTION`), style des comptes d'actu « carte + bandeau » : bandeau du haut (ta marque `NEWS_BRAND`, sujet, date, onglets de chapitres, médias qui défilent), mots-clés en boîtes colorées inclinées, sous-titres en boîte avec le mot prononcé sur fond rouge ; survols satellite (Sentinel-2 cloudless d'EOX, CC BY 4.0 ; `NEWS_MAP`) du lieu principal et des lieux cités (3 max) avec zone surlignée, épingle + étiquette, boussole, coordonnées, altitude et échelle ; photos / vidéos libres dans des panneaux sombres sur fond quadrillé, chiffres clés qui défilent, quiz « ET TOI ? » avec réponses A / B à la fin ; transitions avec « whoosh » (`NEWS_SFX_VOLUME`).
6. **Durée** : 60 s minimum pour une actu (`NEWS_MIN_SECONDS`, visée `NEWS_TARGET_SECONDS`=70), 75 s minimum pour une histoire (`STORY_MIN_SECONDS`, visée 90). Le petit modèle local écrit court : le script est rallongé par l'IA (phrases ajoutées avant la question finale, faits de l'article uniquement) d'après la durée réellement lue par la voix ; sinon le sujet est écarté. Rappel animé « LIKE · COMMENTE · ABONNE-TOI » vers 40 % de la vidéo et à la fin (`NEWS_CTA`).
7. **Histoires vraies envoûtantes** (`STORIES`, `STORY_PER_DAY`) : secrets et mystères documentés (Room 641A, col Dyatlov, Mary Celeste, Opération Mincemeat…), liste intégrée + tes sujets (`STORY_TOPICS` ou `data/stories.txt`, un par ligne, prioritaires). Faits tirés de Wikipédia (français, sinon anglais), l'IA raconte en conteur sans rien inventer (théories présentées comme telles), source citée. Dates clés qui défilent, 2 plans sur 3 en images IA. Bouton « Histoire maintenant » sur la carte. Musique dédiée possible dans `data/music/story`.
8. **Images IA** (`NEWS_AI_IMAGES`) : Pollinations (gratuit, sans clé) ou Hugging Face FLUX.1-schnell (`HF_TOKEN` gratuit), utilisées quand les banques d'images n'ont rien de pertinent ; marquées « IA · ILLUSTRATION » et signalées dans la description.
9. **Voix et récit** : voix neuronale Microsoft Edge gratuite et sans clé (`NEWS_TTS=edge`, voix `NEWS_EDGE_VOICE` pour l'actu, `STORY_EDGE_VOICE` plus grave et posée pour les histoires, sous-titres calés sur les mots réellement lus), voix locale Piper en secours ou au choix (`NEWS_TTS=piper`). Voix expressive (`NEWS_EXPRESSIVE`) : accroche plus énergique, questions qui montent, phrases choc appuyées, suspense ralenti, pauses plus longues avant « Mais… » et avant la question finale ; abréviations lues en toutes lettres (« M. » → Monsieur, « M€ » → millions d'euros) ; traitement « studio » de la voix (`NEWS_VOICE_FX` : grave nettoyé, présence, sifflantes adoucies, compression). Script écrit en 3 étapes (`NEWS_SCRIPT_STEPS`) : faits remis dans l'ordre, récit en texte libre avec une structure imposée (accroche, qui / où / quand, déroulé, rebondissement, question) par un modèle local plus fort (`NEWS_WRITER_MODEL`, qwen2.5:7b), puis mise en forme par le modèle rapide.
6. **Montage** : pastille rouge « ACTU » + titre en boîte blanche en haut, sous-titres mot à mot (mot prononcé en jaune, chiffres en vert), « Source : X » en bas, musique de fond baissée automatiquement sous la voix (mets des mp3 libres de droits dans `data/music/`), son à -14 LUFS.
7. **Publication** : rangée dans **Clips** et mise dans la file d'envoi comme un clip (`NEWS_PUBLISH=false` pour poster à la main). La description cite la source et les auteurs des images.

Tableau de bord : activer / couper, et « Générer une actu maintenant ».

Analyser des vidéos modèles (planches d'images + rythme du montage, rangées dans `data/style/`) :
`docker compose exec worker python -m app.news.probe URL [URL…]` Pour de l'actu jeux vidéo à la place, par exemple : `NEWS_TOPIC=jeux vidéo et GTA 6` et `NEWS_FEEDS=https://www.jeuxvideo.com/rss/rss.xml,https://www.gamekult.com/feed.xml,https://www.millenium.org/rss`.

## Réglages utiles (`.env`)

| Variable | Rôle | Défaut |
|---|---|---|
| `CLIPS_PER_VIDEO` | clips par heure de vidéo | 3 |
| `CLIP_TARGET` / `CLIP_MIN` / `CLIP_MAX` | durée visée / min / max (s), puis ajustée par les stats | 45 / 30 / 58 |
| `ALLOW_PARTS` / `PART_MAX` | découpage en parties au-delà de PART_MAX (déconseillé en Shorts) | false / 90 |
| `POST_GAP_FLOOR` / `DAILY_CAP` | écart minimum imposé (min) / Shorts max par 24 h, répartis sur la journée | 40 / 8 |
| `QUIET_HOURS` | aucun envoi à ces heures, même en mode GTA 6 | 1-8 |
| `PUBLISH_MIN_LLM` / `TOPIC_ONLY` / `QUEUE_MAX_HOURS` | tri de la file : note IA minimum, GTA seulement, fraîcheur max (h) | 7 / true / 36 |
| `PUBLISH_TOP_PCT` / `QUALITY_DAYS` | seuil strict : seuls les N % meilleurs clips des derniers jours partent tout seuls | 20 / 7 |
| `POLISH` | avant l'envoi, l'IA relit chaque clip retenu avec les leçons des stats (2e avis, titre, question) | true |
| `REVIEW_AGE_HOURS` / `BURST_MINUTES` | analyse de chaque Short publié à cet âge / écart sous lequel un envoi compte comme rafale | 24 / 20 |
| `AUTO_PUBLISH` | publier sans validation | true |
| `MIN_MINUTES_BETWEEN_POSTS` / `MAX_POSTS_PER_DAY` | rythme de publication (0 = pas de limite, réglable aussi dans **Clips**) | 90 / 100 |
| `HASHTAGS` | hashtags ajoutés | `#shorts #clip #fr` |
| `HOOK_TITLE` / `HOOK_SECONDS` | accroche en haut de la vidéo / durée (0 = tout le clip) | true / 0 |
| `CLIP_STYLE` | habillage : `magazine` (jeu en haut, facecam en bas, titre 4 s, ligne de source, message du chat, crédit de fin, miniature dessinée) ou `tiktok` (ancien style) | magazine |
| `MAG_CAM_HEIGHT` / `TITLE_SECONDS` / `CREDIT_SECONDS` | facecam en bas (px) / durée du titre / durée du crédit | 730 / 4 / 3 |
| `SOURCE_LINE` / `CHAT_CARD` / `CREDIT_CARD` / `SHORT_THUMBNAIL` | éléments du style magazine | true |
| `TEASER` / `TEASER_SECONDS` | ouverture choc : le passage le plus fort montré en premier, puis le clip depuis le début (gardée ou non selon les stats) | false / 2.5 |
| `CUT_SILENCES` / `SILENCE_MIN` / `SILENCE_MAX_PCT` | coupe les longs blancs (plus de X s) entre deux phrases, au plus N % du clip et jamais sous `CLIP_MIN` | true / 3 / 15 |
| `PUNCH_ZOOM` | zoom rapide sur la réaction (facecam en format split) au moment fort | false |
| `PROGRESS_BAR` | fine barre de progression en haut du clip | false |
| `END_QUESTION` / `END_SECONDS` | question qui pousse à commenter, mise en description YouTube | true / 3.5 |
| `END_CARD` | question affichée à l'écran à la fin (« la suite en partie 2 » sur les parties) | false |
| `SUB_KEYWORDS` | mots forts des sous-titres en vert (sinon blanc, mot prononcé en jaune) | false |
| `TITLE_HASHTAGS` / `TIKTOK_MAX_HASHTAGS` | hashtags ciblés en fin de titre YouTube / nombre max de hashtags sur TikTok | 2 / 2 |
| `KEEP_SOURCE_VIDEOS` | garder les vidéos complètes | false |

## Droits

Clipper le contenu d'autres créateurs sans leur accord peut entraîner des retraits ou des avertissements (strikes). Chaque publication crédite la vidéo d'origine dans la description.
Vidéos d'actu : texte réécrit à partir des faits (source citée), voix et cartes générées, images uniquement sous licence libre (Pexels, Wikimedia Commons avec crédit) ; la musique doit être libre de droits.

## Développement

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Structure : `app/pipeline` (téléchargement, transcription, détection, sous-titres, rendu), `app/publish` (YouTube, TikTok), `app/worker.py` (tâches de fond), `app/web` (interface).
