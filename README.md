# YouTube Video Streaming API

A FastAPI-based service that provides YouTube video streaming, searching, and information retrieval capabilities.

## Features

- **Video Streaming**: Stream YouTube videos with quality selection
- **Format Options**: Download videos in different formats (MP4, MKV, WEBM)
- **Audio Extraction**: Extract audio from videos in MP3 or M4A format
- **Search**: Search YouTube videos with customizable result limits
- **Video Info**: Get detailed information about YouTube videos
- **Cache Management**: Efficient caching with manual clearing option
- **Configurable**: Easy configuration via environment variables or .env file

## Requirements

- Python 3.9+
- FFmpeg (optional, required for high-quality video processing)
- Dependencies listed in requirements.txt

## Installation

1. Clone the repository
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Ensure FFmpeg is installed and available in your PATH (optional)
4. Configure the application (optional):
   - Copy `.env.example` to `.env`
   - Edit the values in `.env` to match your requirements

## Configuration

You can configure the application using environment variables or a `.env` file. Available settings include:

| Variable | Description | Default |
|----------|-------------|---------|
| YT_API_HOST | Server host | 0.0.0.0 |
| YT_API_PORT | Server port | 8000 |
| YT_API_CACHE_DIR | Cache directory | system temp dir |
| YT_API_CACHE_LIMIT_GB | Max cache size in GB | 1.0 |
| YT_API_CACHE_EXPIRY_DAYS | Days until cache files expire | 7 |
| YT_API_AUTO_CLEAN_CACHE | Clean expired files on startup | True |
| YT_API_DEFAULT_QUALITY | Default video quality | 720p |
| YT_API_MAX_SEARCH_RESULTS | Max search results | 50 |
| YT_API_ENABLE_CORS | Enable CORS | True |
| YT_API_CORS_ORIGINS | Allowed origins for CORS | ["*"] |
| YT_API_ENABLE_RATE_LIMIT | Enable rate limiting | True |
| YT_API_RATE_LIMIT_REQUESTS | Max requests per minute | 60 |
| YT_API_RATE_LIMIT_WINDOW | Window size in seconds | 60 |
| YT_API_ADMIN_API_KEY | Admin API key | "" (disabled) |
| YT_API_FFMPEG_PATH | Custom path to FFmpeg | None |

## Usage

### Start the server

```bash
python main.py
```

The API will be available at http://localhost:8000 (or the configured host/port)

### API Endpoints

#### Stream a video

```
GET /video/{video_id}
```

Query parameters:
- `quality`: Video quality (e.g., '1080p', '720p', '480p', '360p')
- `format_type`: Output format - mp4, mkv, webm, mp3, m4a
- `audio_only`: Set to true to extract audio only

#### Search YouTube

```
GET /search?query={search_term}&max_results={number}
```

Query parameters:
- `query`: Search terms
- `max_results`: Maximum number of results (1-50, default: 10)

#### Get video information

```
GET /video/{video_id}/info
```

Returns detailed information about the video including available qualities and streams.

#### Check API Status

```
GET /status
```

Get information about the API including rate limit status and features availability.

#### Admin Endpoints

The following endpoints require the `X-API-Key` header to match the configured `YT_API_ADMIN_API_KEY`:

```
DELETE /admin/cache
```

Clears all cached video files.

```
DELETE /admin/cache/expired
```

Removes only expired cache files.

```
GET /admin/cache/stats
```

Get cache statistics including size, file count, and expiration info.

```
POST /admin/cache/config?expiry_days={days}&max_size_gb={size}&auto_clean={true|false}
```

Dynamically update cache configuration.

## Rate Limiting

The API includes rate limiting to prevent abuse. By default, clients are limited to 60 requests per minute. 
When the rate limit is exceeded, the API returns a 429 Too Many Requests response.

Rate limit headers are included in responses:
- `X-Rate-Limit-Limit`: Maximum number of requests allowed per window
- `X-Rate-Limit-Remaining`: Number of requests remaining in the current window
- `X-Rate-Limit-Reset`: Timestamp when the rate limit window resets

## Example Usage

### Stream a video in 720p
```
GET /video/Z3ZAGBL6UBA?quality=720p
```

### Get audio only in MP3 format
```
GET /video/Z3ZAGBL6UBA?audio_only=true&format_type=mp3
```

### Search for videos
```
GET /search?query=how%20to%20make%20pizza&max_results=5
```

## Dependencies

- FastAPI: Web framework
- PyTubeFix: YouTube video extraction
- yt-dlp: Additional YouTube download capabilities
- uvicorn: ASGI server
- FFmpeg: Audio/video processing
- Brain: Why not lmao.

## Notes

- The API uses a caching system to improve performance for repeated requests (That saves on the TEMP folder of your system)
- High-quality video downloads (>720p) may require combining separate audio and video streams (takes more time to respond)
- Some YouTube videos may have restrictions that prevent downloading

## Security

- Admin endpoints are protected by an API key that must be set in the configuration
- Rate limiting helps prevent abuse of the service
- Cache auto-cleaning prevents disk space issues
