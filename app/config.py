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
    # Shorts courts : sous 35 s la rétention (et donc la diffusion) est bien meilleure
    clip_target: float = 32.0  # durée visée (s)
    clip_min: float = 18.0
    clip_max: float = 58.0  # au-delà, coupé en parties si ALLOW_PARTS, sinon le clip est limité à cette durée
    part_max: float = 90.0  # durée max d'une partie
    allow_parts: bool = False  # « partie 1/2, 2/2 » : la partie 2 n'est presque jamais vue en Shorts
    min_score: float = 0.0
    # Vidéo source pas bonne = 0 clip : un moment noté sous ce score (sur 10) par l'IA n'est même pas monté
    clip_min_llm: float = 6

    # Rendu vidéo
    video_codec: str = "auto"  # auto (GPU intégré VAAPI s'il est accessible, sinon CPU), libx264 ou h264_vaapi
    vaapi_device: str = ""  # ex. /dev/dri/renderD128 pour h264_vaapi
    crf: int = 21
    font_name: str = "Montserrat"
    hook_title: bool = True  # accroche courte en haut de la vidéo
    hook_seconds: float = 0  # durée d'affichage de l'accroche (0 = tout le clip)
    hook_emojis: bool = True  # emojis en lien avec le contenu dans l'accroche
    keep_source_videos: bool = False
    # Ouverture choc : les ~2,5 s les plus fortes du clip montrées en premier, puis le clip depuis le début
    teaser: bool = True
    teaser_seconds: float = 2.5
    # Rythme et engagement
    cut_silences: bool = True  # coupe les blancs de plus de SILENCE_MIN s (« jump cuts »)
    silence_min: float = 1.2
    punch_zoom: bool = True  # zoom rapide sur la réaction au moment fort
    progress_bar: bool = True  # fine barre de progression en haut du clip
    end_question: bool = True  # question / appel à commenter affiché à la fin et mis en légende
    end_seconds: float = 3.5
    title_hashtags: int = 2  # hashtags ajoutés à la fin du titre YouTube (s'il reste de la place)
    tiktok_max_hashtags: int = 5  # TikTok conseille 3 à 5 hashtags ciblés
    # Mise en page : auto (split si une facecam est détectée, sinon classique), split, classic
    layout: str = "auto"
    split_cam_height: int = 640  # hauteur de la facecam en haut du clip (le jeu prend le reste)

    # Publication
    auto_publish: bool = True
    publish_youtube: bool = True
    publish_tiktok: bool = True
    min_minutes_between_posts: int = 90
    max_posts_per_day: int = 100  # par plateforme (YouTube : 100 envois/jour), 0 = pas de limite (réglable aussi dans la page Clips)
    hashtags: str = "#shorts #clip #fr"
    # Qualité plutôt que quantité : une chaîne qui publie en rafale voit chaque Short beaucoup moins poussé.
    post_gap_floor: int = 40  # écart minimum imposé entre deux envois, même si l'interface dit moins (0 = non)
    daily_cap: int = 8  # envois max par 24 h et par plateforme, quels que soient les autres réglages (0 = aucun)
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
    gta6_max_recordings: int = 3  # lives enregistrés en même temps, les autres attendent leur tour
    gta6_chunk_minutes: float = 20  # live découpé en morceaux traités pendant l'enregistrement (0 = non)
    gta6_priority: str = ""  # streamers à enregistrer en premier (noms ou chaînes, séparés par des virgules)
    gta6_min_minutes_between_posts: int = 30
    gta6_publish_anytime: bool = True  # ignore les heures de publication pendant le mode
    gta6_max_posts_per_day: int = 0  # 0 = comme MAX_POSTS_PER_DAY
    gta6_fresh_hours: float = 3  # mode GTA 6 : le score d'un clip est divisé par 2 toutes les N heures

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
