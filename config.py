import os
import tempfile
from typing import Optional, List

try:
    from pydantic_settings import BaseSettings
except ImportError:
    try:
        from pydantic import BaseSettings
    except ImportError:
        raise ImportError("Please install pydantic-settings package: pip install pydantic-settings")

class Settings(BaseSettings):
    # Server settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    
    # Cache settings
    CACHE_DIR: str = os.path.join(tempfile.gettempdir(), "youtube_api_cache")
    CACHE_LIMIT_GB: float = 1.0  # Maximum cache size in GB
    CACHE_EXPIRY_DAYS: int = 7   # Number of days after which cache files expire
    AUTO_CLEAN_CACHE: bool = True  # Automatically clean expired cache files on startup
    
    # Download settings
    DEFAULT_QUALITY: str = "720p"
    MAX_SEARCH_RESULTS: int = 50
    
    # API settings
    ENABLE_CORS: bool = True
    CORS_ORIGINS: List[str] = ["*"]
    
    # FFMPEG settings
    FFMPEG_PATH: Optional[str] = None
    
    # Rate limiting
    ENABLE_RATE_LIMIT: bool = True
    RATE_LIMIT_REQUESTS: int = 60  # Max requests per minute
    RATE_LIMIT_WINDOW: int = 60    # Window size in seconds
    
    # Security settings
    ADMIN_API_KEY: str = "1234"        # Key for admin operations like cache management
    
    # Multi-server settings
    MULTI_SERVER_ENABLED: bool = False  # Enable multi-server mode
    MULTI_SERVER_MAIN: bool = False     # Is this the main server (load balancer)
    MULTI_SERVER_URLS: List[str] = []   # List of worker server URLs (only used when MULTI_SERVER_MAIN is True)
    
    class Config:
        env_file = ".env"
        env_prefix = "YT_API_"

settings = Settings()
