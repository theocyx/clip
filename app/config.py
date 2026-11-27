"""Configuration, lue depuis les variables d'environnement (ou le fichier .env)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Général
    data_dir: Path = Path("/data")
    public_url: str = "http://localhost:8000"  # URL publique (sert aux redirections OAuth)
    contact_email: str = ""  # affiché sur /privacy et /terms (exigé par Google / TikTok)
    admin_user: str = "admin"
    admin_password: str = "change-moi"
    mfa: bool = True  # code à 6 chiffres (Google Authenticator…) en plus du mot de passe
    session_days: float = 30  # durée de connexion avant de redemander mot de passe + code

    # Surveillance des sources
    poll_interval: int = 600  # secondes entre deux vérifications des chaînes

    # Transcription (faster-whisper, CPU)
    whisper_model: str = "small"  # tiny / base / small / medium
    whisper_language: str = "fr"
    whisper_threads: int = 0  # 0 = automatique

    # Vocabulaire donné à Whisper pour bien écrire les noms (GTA, villes, perso…). Vide = aucun.
    whisper_prompt: str = ("GTA 6, GTA VI, Vice City, Leonida, Lucia, Jason, GTA RP, braquage, poursuite, "
                           "flics, Twitch, stream, chat, frérot, wesh, mdr.")

    # Chat Twitch enregistré pendant les lives : ses rafales de réactions aident à trouver les moments forts
    chat_signal: bool = True
    chat_weight: float = 0.5
    chat_delay: float = 6  # secondes entre la scène et la réaction du chat

    # IA locale (Ollama) pour classer les moments et écrire les titres. Vide = désactivé.
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:3b"

    # Clips
    clips_per_video: int = 3  # nombre max de clips par vidéo d'une heure (proportionnel à la durée)
    clips_max_per_video: int = 10
    # Durée calquée sur les TikTok GTA 6 de la chaîne qui marchent le mieux (60 à 68 s, de 40 à 84 s)
    clip_target: float = 62.0  # durée visée (s)
    clip_min: float = 35.0
    clip_max: float = 75.0  # au-delà, coupé en parties si ALLOW_PARTS, sinon le clip est limité à cette durée
    part_max: float = 90.0  # durée max d'une partie
    allow_parts: bool = False  # « partie 1/2, 2/2 » : la partie 2 n'est presque jamais vue en Shorts
    min_score: float = 0.0
    stale_chunk_hours: float = 6  # morceau de live en attente depuis plus de N h : abandonné (0 = jamais)
    # Vidéo source pas bonne = 0 clip : un moment noté sous ce score (sur 10) par l'IA n'est même pas monté
    clip_min_llm: float = 6

    # Rendu vidéo
    video_codec: str = "auto"  # auto (GPU intégré VAAPI s'il est accessible, sinon CPU), libx264 ou h264_vaapi
    vaapi_device: str = ""  # ex. /dev/dri/renderD128 pour h264_vaapi
    crf: int = 21  # qualité (plus bas = plus net et plus lourd) ; GPU : qp = crf - 1
    # Netteté des zones agrandies (facecam, jeu recadré) : 0 = rien, 0.5 = léger, 1 = fort
    sharpen: float = 0.5
    font_name: str = "Montserrat"
    hook_title: bool = True  # accroche courte en haut de la vidéo
    hook_seconds: float = 0  # durée d'affichage de l'accroche (0 = tout le clip)
    hook_emojis: bool = True  # emoji en lien avec le contenu à la fin de l'accroche
    keep_source_videos: bool = False
    # Ouverture choc : les ~2,5 s les plus fortes du clip montrées en premier, puis le clip depuis le début
    # Désactivée par défaut : les TikTok qui marchent sont un seul plan continu, sans flash ni retour en arrière
    teaser: bool = False
    teaser_seconds: float = 2.5
    # Rythme et engagement
    cut_silences: bool = True  # coupe les longs blancs de plus de SILENCE_MIN s (« jump cuts »)
    # Sur GTA l'action sans parole est le contenu : seuls les longs blancs sont coupés, et au plus
    # SILENCE_MAX_PCT % du clip (jamais sous CLIP_MIN)
    silence_min: float = 3.0
    silence_max_pct: float = 15
    # Style des TikTok qui marchent : pas d'effet ajouté (zoom, barre), la vidéo se coupe net sans carte de fin
    punch_zoom: bool = False  # zoom rapide sur la réaction au moment fort
    progress_bar: bool = False  # fine barre de progression en haut du clip
    end_question: bool = True  # question pour les commentaires dans la description YouTube
    end_card: bool = False  # question affichée à l'écran à la fin du clip
    sub_keywords: bool = False  # mots forts des sous-titres en vert (sinon blanc, mot prononcé en jaune)
    end_seconds: float = 3.5
    title_hashtags: int = 2  # hashtags ajoutés à la fin du titre YouTube (s'il reste de la place)
    tiktok_max_hashtags: int = 2  # légende courte : #gta6 #gta, comme les TikTok de la chaîne qui marchent
    # Mise en page : auto (split si une facecam est détectée, sinon classique), split, classic
    layout: str = "auto"
    split_cam_height: int = 614  # hauteur de la facecam en haut du clip (32 %, le jeu prend le reste)
    # Habillage des Shorts (app/pipeline/style.py) : « magazine » = codes relevés le 27/11 sur une chaîne FR de
    # Shorts GTA 6 à 20-400 k vues (jeu en haut, facecam en bas, titre d'ouverture 4 s, ligne de source,
    # message du chat au moment fort, crédit du streamer en fin, miniature dessinée) ; « tiktok » = ancien style
    clip_style: str = "magazine"
    mag_cam_height: int = 730  # hauteur de la facecam EN BAS en style magazine (38 %, le jeu prend le haut)
    title_seconds: float = 4.0  # durée du titre d'ouverture (style magazine)
    source_line: bool = True  # petite ligne « titre du live » (stream Untel) en haut du clip
    chat_card: bool = True  # vrai message du chat Twitch affiché au moment fort
    credit_card: bool = True  # photo + /pseudo + « SUIS-LE SUR TWITCH » à la fin du clip
    credit_seconds: float = 3.0
    short_thumbnail: bool = True  # miniature dessinée envoyée à YouTube (chaîne validée par téléphone requise)

    # Publication
    auto_publish: bool = True
    publish_youtube: bool = True
    publish_tiktok: bool = True
    min_minutes_between_posts: int = 90
    max_posts_per_day: int = 100  # par plateforme (YouTube : 100 envois/jour), 0 = pas de limite (réglable aussi dans la page Clips)
    hashtags: str = "#shorts #clip #fr"
    # Qualité plutôt que quantité : une chaîne qui publie en rafale voit chaque Short beaucoup moins poussé.
    post_gap_floor: int = 40  # écart minimum imposé entre deux envois, même si l'interface dit moins (0 = non)
    # envois max par jour et par plateforme, quels que soient les autres réglages (0 = aucun). 10 : les clips GTA
    # isolés font ~400 vues chacun depuis le 23/11 ; ce sont les rafales (100/jour) qui faisaient 0 vue
    daily_cap: int = 10
    spread_posts: bool = True  # répartit les envois restants du jour sur les heures qui restent
    quiet_hours: str = "1-8"  # jamais d'envoi à ces heures (heure de Paris), même en mode GTA 6. Vide = aucune
    publish_min_llm: float = 7  # clips notés sous ce score (sur 10) par l'IA : pas publiés automatiquement
    # Seuil strict : seuls les clips dans les N % meilleurs scores des QUALITY_DAYS derniers jours partent tout seuls
    publish_top_pct: float = 20  # 0 = pas de seuil relatif
    quality_days: float = 7
    quality_min_ref: int = 12  # clips de référence nécessaires avant d'appliquer le seuil relatif
    polish: bool = True  # avant l'envoi, l'IA relit le clip avec ce qu'elle a appris : 2e avis, titre et question revus
    polish_per_run: int = 4  # clips relus par passage du tri (toutes les 10 min)
    topic_only: bool = True  # ne publie automatiquement que les clips sur GTA (la chaîne reste dans sa niche)
    queue_max_hours: float = 36  # clip en attente depuis plus longtemps : retiré de la file (plus d'actualité)
    # Heures de publication (heure de Paris), plages séparées par des virgules. Vide = à toute heure.
    publish_hours: str = "12-14,18-22"
    timezone: str = "Europe/Paris"
    credit_streamer: bool = True  # nom + chaîne du streamer dans la description
    # Hashtags tendance ajoutés selon le titre du live et ce qui est dit dans le clip (#GTA6, #GTARP, serveur RP…)
    trend_hashtags: bool = True
    # Règles en plus : « mot1|mot2=#Tag1 #Tag2; autre=#Tag3 » (mots = expressions régulières)
    trend_rules: str = ""
    # Serveurs RP reconnus en plus de ceux connus : « Nom du serveur=#HashtagServeur; … »
    rp_servers: str = ""
    max_hashtags: int = 10

    # Anti-doublons : pas deux clips de la même scène (plusieurs streamers sur le même serveur RP)
    dedupe: bool = True
    dedupe_hours: float = 48  # comparaison avec les clips des N dernières heures
    dedupe_similarity: float = 0.3  # part de phrases en commun au-delà de laquelle c'est un doublon

    # Passages sponsorisés (pub, placement de produit, code promo) jamais clippés
    exclude_sponsored: bool = True
    sponsorblock: bool = True  # passages signalés sur SponsorBlock (vidéos YouTube)
    sponsor_pad_before: float = 20  # secondes exclues avant une phrase de pub
    sponsor_pad_after: float = 60  # et après (la pub continue souvent après « merci à … »)
    sponsor_ocr: bool = True  # lit le texte à l'écran (« sponsorisé »…) avant de garder un clip
    sponsor_ocr_every: float = 4  # une image toutes les N secondes

    # Mode « GTA 6 day one » : auto (actif à la date de sortie), on, off. Modifiable aussi dans l'interface.
    gta6_mode: str = "auto"
    gta6_date: str = "2026-11-19"
    gta6_days: int = 3  # durée du mode à partir du jour de sortie
    gta6_hours_before: float = 6  # démarre la veille au soir (lancement à minuit sur certaines plateformes)
    gta6_keywords: str = "gta 6,gta6,gta vi,gtavi,grand theft auto vi,grand theft auto 6"
    gta6_poll_interval: int = 90  # vérification des lives (s) pendant le mode
    gta6_max_recordings: int = 1  # lives enregistrés en même temps, les autres attendent leur tour
    gta6_chunk_minutes: float = 20  # live découpé en morceaux traités pendant l'enregistrement (0 = non)
    gta6_priority: str = ""  # streamers à enregistrer en premier (noms ou chaînes, séparés par des virgules)
    gta6_min_minutes_between_posts: int = 30
    gta6_publish_anytime: bool = True  # ignore les heures de publication pendant le mode
    gta6_max_posts_per_day: int = 0  # 0 = comme MAX_POSTS_PER_DAY
    gta6_fresh_hours: float = 3  # mode GTA 6 : le score d'un clip est divisé par 2 toutes les N heures

    # Vidéos d'actu générées de A à Z (voir app/news) : sujet trouvé dans des flux RSS, script écrit par l'IA
    # locale, voix off locale (Piper), images libres de droits, montage vertical et sous-titres mot à mot.
    news: bool = True  # réglable aussi depuis le tableau de bord
    news_per_day: int = 3  # vidéos d'actu créées par jour (heure de Paris)
    news_interval_minutes: int = 60  # recherche d'un nouveau sujet toutes les N minutes
    news_hours: str = "7-22"  # heures de Paris où l'on cherche un sujet (vide = à toute heure)
    news_topic: str = "actualité France et monde"  # thème de la chaîne, donné à l'IA
    # Flux RSS/Atom séparés par des virgules (actu générale par défaut ; voir le README pour l'actu jeux vidéo)
    news_feeds: str = ("https://www.francetvinfo.fr/titres.rss,https://www.lemonde.fr/rss/une.xml,"
                       "https://www.20minutes.fr/feeds/rss-une.xml,"
                       "https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr")
    news_keywords: str = ""  # seuls les articles dont le titre/résumé contient un de ces mots (vide = tous)
    news_exclude: str = ("promo,bon plan,soldes,black friday,code promo,horoscope,météo,loto,euromillions,"
                         "nécrologie,obsèques,mots croisés,podcast,vidéo :,en direct")
    news_max_age_hours: float = 24  # article plus vieux : ignoré
    news_min_score: float = 6  # note IA (sur 10) minimum du sujet pour en faire une vidéo
    news_publish: bool = True  # envoie les vidéos d'actu dans la file de publication (sinon : seulement dans Clips)
    news_target_seconds: float = 70  # durée visée de la voix off d'une actu (secondes)
    news_min_seconds: float = 60  # plus court : script rallongé, sinon sujet écarté
    news_script_steps: bool = True  # écriture en 3 étapes : faits dans l'ordre → récit libre → mise en forme
    news_writer_model: str = "qwen2.5:7b"  # modèle plus fort pour écrire le récit (vide = le modèle habituel)
    news_llm_timeout: float = 1200  # s : l'IA est partagée avec l'analyse des clips, la réponse peut attendre
    # voix : edge (voix neuronale Microsoft gratuite, sans clé, très naturelle) ou piper (100 % locale, plus robotique)
    news_tts: str = "edge"
    news_edge_voice: str = "fr-FR-HenriNeural"  # voix d'actu (fr-FR-DeniseNeural, fr-FR-VivienneMultilingualNeural…)
    story_edge_voice: str = "fr-FR-RemyMultilingualNeural"  # voix de conteur pour les histoires
    story_edge_pitch: str = "-2Hz"  # un peu plus grave pour les histoires
    # Voix essayées par l'amélioration continue (la meilleure d'après les stats devient la voix habituelle)
    news_voices: str = "fr-FR-HenriNeural,fr-FR-RemyMultilingualNeural,fr-FR-VivienneMultilingualNeural"
    story_voices: str = "fr-FR-RemyMultilingualNeural,fr-FR-HenriNeural"
    news_expressive: bool = True  # intonation, débit et pauses qui changent selon la phrase (accroche, question…)
    news_voice_fx: bool = True  # traitement « studio » de la voix : grave nettoyé, présence, sifflantes, compression
    news_voice: str = "fr_FR-tom-medium"  # voix Piper (fr_FR-siwis-medium, fr_FR-upmc-medium, fr_FR-gilles-low…)
    news_voice_speed: float = 1.12  # débit de la voix (1 = normal) : un peu rapide, comme les comptes d'actu
    # Histoires vraies envoûtantes (voir app/news/stories.py) : faits tirés de Wikipédia, même chaîne de montage
    stories: bool = True  # réglable aussi depuis le tableau de bord
    story_per_day: int = 1  # histoires par jour (en plus des actus)
    story_target_seconds: float = 90  # durée visée de la voix off d'une histoire
    story_min_seconds: float = 75
    story_voice_speed: float = 1.0  # voix plus posée que pour l'actu
    story_topics: str = ""  # tes sujets, séparés par des virgules (aussi : fichier data/stories.txt, un par ligne)
    story_builtin: bool = True  # piocher aussi dans la liste intégrée (Room 641A, col Dyatlov, Mary Celeste…)
    news_cta: bool = True  # rappel animé « LIKE · COMMENTE · ABONNE-TOI » (vers 40 % et à la fin)
    news_brand: str = "ACTU EN BREF"  # nom de marque dans le bandeau du haut (le tien, jamais celui d'un autre compte)
    news_badge: str = "ACTU"  # pastille rouge au-dessus du titre (vide = aucune)
    news_accent: str = "#FFD400"  # couleur des cartes générées quand aucune image libre ne convient
    pexels_api_key: str = ""  # clé gratuite pexels.com/api : vidéos et photos libres de droits (recommandé)
    pixabay_api_key: str = ""  # clé gratuite sur pixabay.com/api/docs (photos + vidéos libres)
    # Images générées par IA quand les banques d'images n'ont rien de pertinent (et une sur deux pour les histoires) :
    # pollinations (gratuit, sans clé), huggingface (HF_TOKEN gratuit, FLUX.1-schnell), off
    news_ai_images: str = "pollinations"
    news_ai_gap: float = 16  # s entre deux images Pollinations (gratuit : une image toutes les ~15 s, sinon refus 402)
    news_ai_style: str = "cinematic documentary photo, dramatic lighting, highly detailed, realistic, no text"
    hf_token: str = ""
    news_openverse: bool = True  # images libres via Openverse (sans clé)
    news_wikimedia: bool = True  # images sous licence libre de Wikimedia Commons (auteur crédité en description)
    news_motion: bool = True  # habillage animé (cartes 3D, mots animés, chiffres, transitions, whoosh)
    news_map: bool = True  # survol « vu d'en haut » du lieu de l'actu en ouverture
    news_map_tiles: str = ("https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg")
    news_sfx_volume: float = 0.35  # volume des « whoosh » des transitions (0 = aucun)
    news_music_volume: float = 0.14  # volume de la musique de fond (fichiers mp3 libres de droits dans data/music)

    # Notifications (laisser vide pour désactiver)
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_published: bool = True
    notify_errors: bool = True

    # Santé du serveur : alertes par notification (disque, température, mémoire, services)
    health: bool = True
    health_interval: int = 300  # secondes entre deux vérifications
    health_disk_pct: float = 90  # alerte au-delà de ce % de disque utilisé…
    health_disk_min_gb: float = 5  # … ou sous ce nombre de Go libres
    health_temp_max: float = 90  # °C (le Ryzen 4700U se protège vers 105 °C)
    health_ram_pct: float = 92
    health_repeat_hours: float = 6  # rappel tant que le problème dure (0 = une seule fois)
    health_worker_timeout: int = 600  # worker sans signe de vie depuis N secondes = planté
    health_web_url: str = "http://web:8000/healthz"  # vide = pas de vérification de l'interface

    # Découverte automatique des streamers FR qui jouent à GTA
    discover: bool = True
    discover_games: str = "Grand Theft Auto VI,Grand Theft Auto V"  # catégories Twitch
    discover_youtube: bool = True
    discover_youtube_queries: str = "gta 6 fr,gta rp fr"  # recherches de lives YouTube
    discover_min_viewers: int = 150  # spectateurs minimum pour être ajouté
    discover_max_sources: int = 10  # plafond de chaînes ajoutées automatiquement (CPU)
    discover_per_run: int = 2  # ajouts max par recherche
    discover_interval_minutes: int = 30  # 10 min max pendant le mode GTA 6
    discover_expire_days: int = 7  # chaîne auto retirée si plus vue en live GTA depuis N jours (0 = jamais)

    # Sauvegarde de la base et ménage du disque
    backup_keep: int = 7  # nombre de sauvegardes quotidiennes gardées (0 = pas de sauvegarde)
    source_keep_days: int = 3  # vidéos sources (échecs, lives enregistrés) supprimées après N jours
    clip_keep_days: int = 14  # fichiers des clips déjà publiés supprimés après N jours (0 = jamais)
    # fichiers des clips jamais publiés (retirés par le tri, pas postés à la main) supprimés après N jours (0 = jamais)
    unpublished_keep_days: int = 5

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_privacy: str = "public"  # public / unlisted / private
    youtube_category: str = "20"  # 20 = Gaming, 24 = Divertissement

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_mode: str = "direct"  # direct (publication) ou draft (brouillon dans l'appli TikTok)
    tiktok_privacy: str = "SELF_ONLY"  # SELF_ONLY tant que l'app n'est pas auditée, puis PUBLIC_TO_EVERYONE
    tiktok_stats: bool = False  # demande aussi le scope video.list (à activer dans l'app TikTok) pour lire les vues

    # Apprentissage : relevé des stats des clips publiés et ajustement automatique du montage
    learning: bool = True
    stats_interval_hours: float = 3
    stats_days: int = 30  # suivi des publications pendant N jours
    learn_min_clips: int = 10  # nombre de clips mesurés avant de modifier quoi que ce soit
    learn_age_hours: float = 72  # âge auquel on compare les clips entre eux
    review_age_hours: float = 24  # chaque Short est analysé (et signalé) une fois cet âge atteint
    burst_minutes: float = 20  # Short publié moins de N min après le précédent : « rafale », écarté de l'apprentissage
    explore: float = 0.15  # variation aléatoire de la durée visée (±15 %) pour continuer à tester
    # Amélioration continue : une part des vidéos essaie une autre version d'UN réglage de montage (voix, débit,
    # zoom, sous-titres…) ; la version qui fait les meilleures stats devient la version par défaut
    experiments: bool = True
    experiment_rate: float = 0.25  # part des vidéos qui essaient une autre version
    experiment_min: int = 4  # vidéos mesurées par version avant de pouvoir la déclarer meilleure
    sub_pop: bool = True  # sous-titres qui apparaissent avec un petit effet de rebond

    # Croissance de la chaîne (app/growth.py) : on double ce qui marche, on coupe ce qui ne marche pas
    subscribe_link: bool = True  # lien « abonne-toi » en un clic dans les descriptions YouTube
    growth_mix: bool = True  # le format qui fait le plus de vues passe devant, et est produit plus souvent
    mix_days: float = 7  # Shorts des N derniers jours comparés par format (clips GTA, actus, histoires)
    mix_min: int = 4  # Shorts mesurés par format avant de changer quoi que ce soit
    mix_test_days: float = 3  # format qui ne marche pas : plus produit qu'une fois tous les N jours (pour retester)
    news_max_per_day: int = 5  # au plus, quand les actus marchent mieux que le reste
    story_max_per_day: int = 4
    # Réserve : un clip GTA juste sous le seuil appris (note IA, top %) n'est plus jeté ; il part quand la file
    # serait vide (le 27/11 : 87 % des clips retirés, plus un seul clip GTA en file, des créneaux perdus)
    reserve: bool = True
    reserve_max: int = 20  # clips gardés en réserve au plus (les mieux notés)
    # Les streamers dont les clips marchent le mieux sont enregistrés et traités en premier
    source_priority: bool = True
    clip_cta: bool = False  # appel « abonne-toi » à l'écran en fin de clip (testé par l'amélioration continue)
    # Interactions (app/interact.py) : 0 commentaire sur les 189 premiers Shorts (27/11)
    first_comment: bool = True  # la chaîne pose la question du clip en 1er commentaire (reconnexion YouTube requise)
    first_comment_delay: float = 3  # minutes après l'envoi (le temps que YouTube enregistre la vidéo)
    share_line: bool = True  # « 📤 Envoie-le à un pote… » dans les descriptions YouTube
    # Compilations longues hebdomadaires (app/compile.py) : les seules vidéos qui font des heures de visionnage
    compil: bool = True  # réglable aussi depuis la page Statistiques
    compil_kinds: str = "clip"  # clip (meilleurs moments GTA), story (histoires), news, séparés par des virgules
    compil_day: int = 6  # jour de publication (0 = lundi … 6 = dimanche)
    compil_hour: int = 18  # heure de Paris à partir de laquelle la compilation est montée puis publiée
    compil_days: float = 7  # meilleurs Shorts des N derniers jours
    compil_max: int = 15  # Shorts au plus dans une compilation
    compil_min: int = 6  # en dessous, pas de compilation cette semaine
    compil_min_seconds: float = 300  # 5 min au moins (sous 3 min, YouTube la prendrait pour un Short)
    compil_category: str = "20"  # 20 = Gaming (histoires : 24 = Divertissement)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clip.db"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.videos_dir, s.clips_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
