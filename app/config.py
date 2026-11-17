"""Configuration, lue depuis les variables d'environnement (ou le fichier .env)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Général
    data_dir: Path = Path("/data")
    public_url: str = "http://localhost:8000"  # URL publique (sert aux redirections OAuth)
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

    # IA locale (Ollama) pour classer les moments et écrire les titres. Vide = désactivé.
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:3b"

    # Clips
    clips_per_video: int = 3  # nombre max de clips par vidéo d'une heure (proportionnel à la durée)
    clips_max_per_video: int = 10
    clip_target: float = 60.0  # durée visée (s)
    clip_min: float = 35.0
    clip_max: float = 180.0  # au-delà, le clip est coupé en parties
    part_max: float = 90.0  # durée max d'une partie
    min_score: float = 0.0

    # Rendu vidéo
    video_codec: str = "libx264"  # ou h264_vaapi (GPU intégré Intel/AMD)
    vaapi_device: str = ""  # ex. /dev/dri/renderD128 pour h264_vaapi
    crf: int = 21
    font_name: str = "Montserrat"
    hook_title: bool = True  # accroche courte en haut de la vidéo
    hook_seconds: float = 0  # durée d'affichage de l'accroche (0 = tout le clip)
    hook_emojis: bool = True  # emojis en lien avec le contenu dans l'accroche
    keep_source_videos: bool = False
    # Mise en page : auto (split si une facecam est détectée, sinon classique), split, classic
    layout: str = "auto"
    split_cam_height: int = 640  # hauteur de la facecam en haut du clip (le jeu prend le reste)

    # Publication
    auto_publish: bool = True
    publish_youtube: bool = True
    publish_tiktok: bool = True
    min_minutes_between_posts: int = 90
    max_posts_per_day: int = 6  # par plateforme (quota YouTube : ~6 envois/jour)
    hashtags: str = "#shorts #clip #fr"
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

    # Notifications (laisser vide pour désactiver)
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_published: bool = True
    notify_errors: bool = True

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
    stats_interval_hours: float = 6
    stats_days: int = 30  # suivi des publications pendant N jours
    learn_min_clips: int = 10  # nombre de clips mesurés avant de modifier quoi que ce soit
    learn_age_hours: float = 72  # âge auquel on compare les clips entre eux
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
