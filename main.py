from fastapi import FastAPI, HTTPException, Query, Depends, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pytubefix import YouTube
import io
import os
import tempfile
import subprocess
import shutil
import hashlib
from functools import lru_cache
import asyncio
import time
from typing import Optional, Dict, Tuple, List
import yt_dlp
from enum import Enum
from datetime import datetime, timedelta
import json

from config import settings

app = FastAPI(title="YouTube Video Streaming API")

if settings.ENABLE_CORS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

if settings.ENABLE_RATE_LIMIT:
    rate_limit_data = {}
    
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if settings.ADMIN_API_KEY and request.headers.get("X-API-Key") == settings.ADMIN_API_KEY:
            return await call_next(request)
            
        client_ip = request.client.host
        current_time = time.time()
        
        if client_ip in rate_limit_data:
            rate_limit_data[client_ip] = [ts for ts in rate_limit_data[client_ip] 
                                       if ts > current_time - settings.RATE_LIMIT_WINDOW]
        else:
            rate_limit_data[client_ip] = []
        
        if len(rate_limit_data[client_ip]) >= settings.RATE_LIMIT_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please try again later."}
            )
        
        rate_limit_data[client_ip].append(current_time)
        
        response = await call_next(request)
        
        remaining = settings.RATE_LIMIT_REQUESTS - len(rate_limit_data[client_ip])
        response.headers["X-Rate-Limit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
        response.headers["X-Rate-Limit-Remaining"] = str(max(0, remaining))
        response.headers["X-Rate-Limit-Reset"] = str(int(current_time + settings.RATE_LIMIT_WINDOW))
        
        return response

os.makedirs(settings.CACHE_DIR, exist_ok=True)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_admin_api_key(api_key: str = Depends(api_key_header)):
    if not settings.ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Admin API is not configured")
    
    if api_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return api_key

FFMPEG_PATH = settings.FFMPEG_PATH or shutil.which("ffmpeg")
FFMPEG_AVAILABLE = FFMPEG_PATH is not None

video_cache: Dict[str, Dict[str, str]] = {}

def get_cache_key(video_id: str, quality: Optional[str] = None) -> str:
    key = f"{video_id}_{quality if quality else 'best'}"
    return hashlib.md5(key.encode()).hexdigest()

def is_cache_file_expired(file_path: str) -> bool:
    if not os.path.exists(file_path):
        return False
    
    file_mod_time = os.path.getmtime(file_path)
    mod_datetime = datetime.fromtimestamp(file_mod_time)
    
    expiry_time = mod_datetime + timedelta(days=settings.CACHE_EXPIRY_DAYS)
    
    return datetime.now() > expiry_time

def clean_expired_cache_files() -> int:
    cleaned_count = 0
    for filename in os.listdir(settings.CACHE_DIR):
        file_path = os.path.join(settings.CACHE_DIR, filename)
        if os.path.isfile(file_path) and is_cache_file_expired(file_path):
            try:
                os.unlink(file_path)
                cleaned_count += 1
            except Exception as e:
                print(f"Error removing expired file {file_path}: {str(e)}")
    return cleaned_count

if settings.AUTO_CLEAN_CACHE:
    try:
        print("Checking for expired cache files...")
        cleaned_count = clean_expired_cache_files()
        print(f"Cleaned {cleaned_count} expired cache files")
    except Exception as e:
        print(f"Error cleaning cache on startup: {str(e)}")

async def get_or_create_cached_file(video_id: str, quality: Optional[str] = None) -> Tuple[str, bool]:
    cache_key = get_cache_key(video_id, quality)
    cache_path = os.path.join(settings.CACHE_DIR, f"{cache_key}.mp4")
    
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0 and not is_cache_file_expired(cache_path):
        return cache_path, False
    
    return cache_path, True

async def combine_audio_video(video_path: str, audio_path: str, output_path: str):
    cmd = [
        FFMPEG_PATH, "-i", video_path, "-i", audio_path,
        "-c:v", "copy", "-c:a", "aac", output_path,
        "-y"
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    
    if process.returncode != 0:
        raise Exception("FFmpeg failed to combine audio and video streams")

async def download_with_ytdlp(video_id: str, quality: str, output_path: str):
    height = int(quality.replace('p', ''))
    base_output_path = os.path.splitext(output_path)[0]
    
    ydl_opts = {
        'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]',
        'outtmpl': f"{base_output_path}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': False,
        'merge_output_format': 'mp4',
    }
    
    loop = asyncio.get_event_loop()
    
    async def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return await loop.run_in_executor(
                None, 
                lambda: ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            )
    
    result = await _download()
    
    if result != 0:
        raise Exception("yt-dlp failed to download video")
    
    actual_file = None
    extensions = ['.mp4', '.mkv', '.webm', '.mp4.mkv', '.mp4.webm']
    for ext in extensions:
        potential_file = f"{base_output_path}{ext}"
        if os.path.exists(potential_file):
            actual_file = potential_file
            break
    
    if not actual_file:
        raise FileNotFoundError(f"Could not find downloaded file for {video_id}")
    
    if actual_file != output_path:
        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(actual_file, output_path)
    
    return output_path

class FormatType(str, Enum):
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"
    AUDIO_MP3 = "mp3"
    AUDIO_M4A = "m4a"

async def download_audio_only(video_id: str, output_path: str, format_type: str = "m4a"):
    base_output_path = os.path.splitext(output_path)[0]
    audio_output = f"{base_output_path}.{format_type}"
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{base_output_path}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': False,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': format_type,
            'preferredquality': '192',
        }] if format_type in ['mp3', 'm4a'] else [],
    }
    
    loop = asyncio.get_event_loop()
    
    async def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return await loop.run_in_executor(
                None, 
                lambda: ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            )
    
    result = await _download()
    
    if result != 0:
        raise Exception(f"yt-dlp failed to download audio")
    
    actual_file = None
    extensions = [f'.{format_type}', '.webm', '.m4a', '.mp3']
    for ext in extensions:
        potential_file = f"{base_output_path}{ext}"
        if os.path.exists(potential_file):
            actual_file = potential_file
            break
    
    if not actual_file:
        raise FileNotFoundError(f"Could not find downloaded audio file for {video_id}")
    
    if actual_file != audio_output:
        if os.path.exists(audio_output):
            os.remove(audio_output)
        os.rename(actual_file, audio_output)
    
    return audio_output

@app.get("/video/{video_id}")
async def stream_youtube_video(
    video_id: str, 
    quality: str = Query(None, description=f"Desired video quality (e.g., '1080p', '720p', '480p', '360p'). Default: {settings.DEFAULT_QUALITY}"),
    format_type: FormatType = Query(FormatType.MP4, description="Video format type"),
    audio_only: bool = Query(False, description="Get audio-only stream")
):
    if quality is None:
        quality = settings.DEFAULT_QUALITY
        
    try:
        cache_key = f"{video_id}_{quality if quality else 'best'}"
        if audio_only:
            cache_key += f"_audio_{format_type}"
        cache_key = hashlib.md5(cache_key.encode()).hexdigest()
        
        file_ext = format_type.value
        cache_path = os.path.join(settings.CACHE_DIR, f"{cache_key}.{file_ext}")
        
        is_new = not (os.path.exists(cache_path) and os.path.getsize(cache_path) > 0)
        
        if is_new:
            if audio_only:
                await download_audio_only(video_id, cache_path, format_type)
            else:
                quality_level = 0
                if quality:
                    try:
                        quality_level = int(quality.replace('p', ''))
                    except ValueError:
                        pass
                
                if format_type != FormatType.MP4 or quality_level > 720:
                    try:
                        await download_with_ytdlp(video_id, quality, cache_path)
                    except Exception as e:
                        print(f"Error with yt-dlp: {str(e)}, falling back to PyTubeFix")
                        if format_type == FormatType.MP4:
                            await download_with_pytube(video_id, quality, cache_path)
                        else:
                            raise HTTPException(status_code=400, detail=f"Format {format_type} requires yt-dlp which failed. Error: {str(e)}")
                else:
                    await download_with_pytube(video_id, quality, cache_path)

        if not os.path.exists(cache_path):
            base_path = os.path.splitext(cache_path)[0]
            for ext in [f'.{format_type}', '.mp4', '.mkv', '.webm', '.mp4.mkv', '.mp4.webm', '.m4a', '.mp3']:
                alt_path = f"{base_path}{ext}"
                if os.path.exists(alt_path):
                    cache_path = alt_path
                    break
            else:
                raise HTTPException(status_code=404, detail=f"File not found. Download may have failed.")
        
        def iterfile():
            with open(cache_path, 'rb') as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        
        mime_types = {
            "mp4": "video/mp4",
            "mkv": "video/x-matroska",
            "webm": "video/webm",
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4"
        }
        
        ext = os.path.splitext(cache_path)[1][1:]
        mime_type = mime_types.get(ext, "application/octet-stream")
        
        return StreamingResponse(
            iterfile(),
            media_type=mime_type,
            headers={
                "Content-Disposition": f"inline; filename={video_id}{os.path.splitext(cache_path)[1]}",
                "Content-Type": mime_type
            }
        )
    
    except Exception as e:
        import traceback
        error_detail = str(e)
        print(f"Error in stream_youtube_video: {error_detail}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error retrieving content: {error_detail}")

@app.get("/search", response_model=List[Dict])
async def search_youtube(
    query: str = Query(..., description="Search terms"),
    max_results: int = Query(10, description=f"Maximum number of results to return (1-{settings.MAX_SEARCH_RESULTS})", ge=1, le=settings.MAX_SEARCH_RESULTS)
):
    try:
        loop = asyncio.get_event_loop()
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'extract_flat': True,
            'skip_download': True,
            'format': 'best',
        }
        
        async def _search():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                url = f"ytsearch{max_results}:{query}"
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                return info
        
        search_results = await _search()
        
        if not search_results or 'entries' not in search_results:
            return []
        
        formatted_results = []
        for entry in search_results['entries']:
            if entry:
                formatted_results.append({
                    "id": entry.get('id'),
                    "title": entry.get('title'),
                    "uploader": entry.get('uploader'),
                    "duration": entry.get('duration'),
                    "view_count": entry.get('view_count'),
                    "thumbnail": entry.get('thumbnail'),
                    "url": f"https://www.youtube.com/watch?v={entry.get('id')}"
                })
        
        return formatted_results
    
    except Exception as e:
        import traceback
        error_detail = str(e)
        print(f"Error in search_youtube: {error_detail}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error searching YouTube: {error_detail}")

@app.get("/video/{video_id}/info")
async def get_video_info(video_id: str):
    try:
        available_formats = []
        available_qualities = set()
        
        ydl_opts = {
            'format': 'best',
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'skip_download': True,
            'listformats': True,
        }
        
        loop = asyncio.get_event_loop()
        
        class FormatCollector:
            def __init__(self):
                self.formats = []

            def debug(self, msg):
                if "format code" in msg and "resolution" in msg:
                    parts = msg.split()
                    if len(parts) >= 5:
                        resolution = parts[3]
                        if 'x' in resolution:
                            height = resolution.split('x')[1]
                            if height.isdigit():
                                available_qualities.add(f"{height}p")

            def warning(self, msg):
                # Empty method to satisfy yt-dlp logger interface
                pass

            def error(self, msg):
                # Empty method to satisfy yt-dlp logger interface
                pass
        
        collector = FormatCollector()
        
        async def _get_formats():
            with yt_dlp.YoutubeDL({'logger': collector, **ydl_opts}) as ydl:
                return await loop.run_in_executor(
                    None, 
                    lambda: ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                )
        
        info = await _get_formats()
        
        yt = YouTube(f"https://www.youtube.com/watch?v={video_id}")
        
        progressive_streams = yt.streams.filter(progressive=True, file_extension='mp4')
        adaptive_streams = yt.streams.filter(adaptive=True, file_extension='mp4', type="video")
        
        for stream in progressive_streams:
            if stream.resolution:
                available_qualities.add(stream.resolution)
        
        for stream in adaptive_streams:
            if stream.resolution:
                available_qualities.add(stream.resolution)
        
        for quality in ["1080p", "1440p", "2160p"]:
            if quality not in available_qualities:
                available_qualities.add(quality)
        
        return {
            "title": info.get('title', yt.title),
            "author": info.get('uploader', yt.author),
            "length": info.get('duration', yt.length),
            "views": info.get('view_count', yt.views),
            "thumbnail_url": info.get('thumbnail', yt.thumbnail_url),
            "available_qualities": sorted(list(available_qualities), 
                                         key=lambda x: int(x.replace('p', '')), 
                                         reverse=True),
            "progressive_streams": [
                {
                    "resolution": stream.resolution,
                    "mime_type": stream.mime_type,
                    "type": stream.type
                } 
                for stream in progressive_streams
            ],
            "adaptive_streams": [
                {
                    "resolution": stream.resolution,
                    "mime_type": stream.mime_type,
                    "type": stream.type,
                    "fps": stream.fps
                }
                for stream in adaptive_streams
            ]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving video info: {str(e)}")

@app.delete("/admin/cache", dependencies=[Depends(verify_admin_api_key)])
async def clear_cache():
    try:
        for filename in os.listdir(settings.CACHE_DIR):
            file_path = os.path.join(settings.CACHE_DIR, filename)
            if os.path.isfile(file_path):
                os.unlink(file_path)
        return {"message": "Cache cleared successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error clearing cache: {str(e)}")

@app.delete("/admin/cache/expired", dependencies=[Depends(verify_admin_api_key)])
async def clear_expired_cache():
    try:
        cleaned_count = clean_expired_cache_files()
        return {"message": f"Successfully cleaned {cleaned_count} expired cache files"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error clearing expired cache: {str(e)}")

@app.get("/admin/cache/stats", dependencies=[Depends(verify_admin_api_key)])
async def get_cache_stats():
    try:
        cache_size = 0
        file_count = 0
        expired_count = 0
        
        for filename in os.listdir(settings.CACHE_DIR):
            file_path = os.path.join(settings.CACHE_DIR, filename)
            if os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                cache_size += file_size
                file_count += 1
                
                if is_cache_file_expired(file_path):
                    expired_count += 1
        
        cache_size_mb = cache_size / (1024 * 1024)
        cache_size_gb = cache_size_mb / 1024
        
        return {
            "cache_directory": settings.CACHE_DIR,
            "file_count": file_count,
            "expired_files_count": expired_count,
            "cache_size_bytes": cache_size,
            "cache_size_mb": round(cache_size_mb, 2),
            "cache_size_gb": round(cache_size_gb, 4),
            "max_cache_size_gb": settings.CACHE_LIMIT_GB,
            "cache_expiry_days": settings.CACHE_EXPIRY_DAYS,
            "cache_usage_percent": round((cache_size_gb / settings.CACHE_LIMIT_GB) * 100, 2) if settings.CACHE_LIMIT_GB > 0 else 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting cache stats: {str(e)}")

@app.post("/admin/cache/config", dependencies=[Depends(verify_admin_api_key)])
async def update_cache_config(
    expiry_days: int = Query(None, description="Number of days after which cache files expire", ge=1),
    max_size_gb: float = Query(None, description="Maximum cache size in GB", gt=0),
    auto_clean: bool = Query(None, description="Automatically clean expired cache on startup")
):
    try:
        if expiry_days is not None:
            settings.CACHE_EXPIRY_DAYS = expiry_days
        
        if max_size_gb is not None:
            settings.CACHE_LIMIT_GB = max_size_gb
            
        if auto_clean is not None:
            settings.AUTO_CLEAN_CACHE = auto_clean
            
        return {
            "message": "Cache settings updated successfully",
            "current_settings": {
                "cache_expiry_days": settings.CACHE_EXPIRY_DAYS,
                "max_cache_size_gb": settings.CACHE_LIMIT_GB,
                "auto_clean_cache": settings.AUTO_CLEAN_CACHE
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating cache config: {str(e)}")

async def download_with_pytube(video_id: str, quality: Optional[str], output_path: str):
    yt = YouTube(f"https://www.youtube.com/watch?v={video_id}")
    progressive_streams = yt.streams.filter(progressive=True, file_extension='mp4')
    high_quality_requested = quality and quality in ["1080p", "1440p", "2160p"] and FFMPEG_AVAILABLE
    
    if high_quality_requested:
        video_stream = yt.streams.filter(
            adaptive=True, 
            file_extension='mp4', 
            resolution=quality,
            type="video"
        ).first()
        
        if not video_stream:
            video_stream = progressive_streams.order_by('resolution').last()
            with open(output_path, 'wb') as f:
                video_stream.stream_to_buffer(f)
        else:
            audio_stream = yt.streams.filter(
                adaptive=True,
                type="audio",
                file_extension="mp4"
            ).order_by('abr').last()
            
            if not audio_stream:
                raise HTTPException(status_code=404, detail="No suitable audio stream found")
            
            temp_video = os.path.join(settings.CACHE_DIR, f"{video_id}_video_temp.mp4")
            temp_audio = os.path.join(settings.CACHE_DIR, f"{video_id}_audio_temp.mp4")
            
            with open(temp_video, 'wb') as f:
                video_stream.stream_to_buffer(f)
            
            with open(temp_audio, 'wb') as f:
                audio_stream.stream_to_buffer(f)
            
            await combine_audio_video(temp_video, temp_audio, output_path)
            
            os.remove(temp_video)
            os.remove(temp_audio)
    else:
        if quality:
            video_stream = progressive_streams.filter(resolution=quality).first()
            if not video_stream:
                video_stream = progressive_streams.order_by('resolution').last()
        else:
            video_stream = progressive_streams.order_by('resolution').last()
        
        if not video_stream:
            raise HTTPException(status_code=404, detail="No suitable video stream found")
        
        with open(output_path, 'wb') as f:
            video_stream.stream_to_buffer(f)

@app.get("/status")
async def get_api_status(request: Request):
    client_ip = request.client.host
    current_time = time.time()
    
    remaining = settings.RATE_LIMIT_REQUESTS
    if settings.ENABLE_RATE_LIMIT and client_ip in rate_limit_data:
        valid_requests = [ts for ts in rate_limit_data[client_ip] 
                         if ts > current_time - settings.RATE_LIMIT_WINDOW]
        remaining = max(0, settings.RATE_LIMIT_REQUESTS - len(valid_requests))
    
    return {
        "status": "online",
        "version": "1.0.0",
        "rate_limit": {
            "enabled": settings.ENABLE_RATE_LIMIT,
            "limit": settings.RATE_LIMIT_REQUESTS,
            "window_seconds": settings.RATE_LIMIT_WINDOW,
            "remaining": remaining,
            "reset": int(current_time + settings.RATE_LIMIT_WINDOW)
        },
        "cache": {
            "enabled": True,
            "expiry_days": settings.CACHE_EXPIRY_DAYS
        },
        "features": {
            "ffmpeg_available": FFMPEG_AVAILABLE,
            "high_quality_support": FFMPEG_AVAILABLE
        }
    }

@app.get("/embed/{video_id}", response_class=HTMLResponse)
async def embed_youtube_video(
    video_id: str, 
    quality: str = Query(None, description=f"Desired video quality (e.g., '1080p', '720p', '480p', '360p'). Default: {settings.DEFAULT_QUALITY}"),
    audio_only: bool = Query(False, description="Embed audio-only player")
):
    try:
        video_info = await get_video_info(video_id)

        if quality is None:
            quality = settings.DEFAULT_QUALITY

        source_url = f"/video/{video_id}?quality={quality}"
        if audio_only:
            source_url += "&audio_only=true"

        html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{video_info['title']} - YouTube Embed</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/plyr/3.7.8/plyr.min.css">
    <style>
        body {{ 
            margin: 0; 
            font-family: Arial, sans-serif; 
            background-color: #000; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            min-height: 100vh;
        }}
        .plyr {{ 
            max-width: 100%; 
            width: 100%; 
            max-height: 100vh; 
        }}
        .video-info {{
            color: white;
            text-align: center;
            padding: 10px;
            background-color: rgba(0,0,0,0.7);
        }}
    </style>
</head>
<body>
    <div>
        <{'video' if not audio_only else 'audio'} 
            id="player" 
            controls 
            crossorigin 
            playsinline 
            {'poster="' + video_info.get('thumbnail_url', '') + '"' if not audio_only else ''}>
            <source 
                src="{source_url}" 
                type="{'video/mp4' if not audio_only else 'audio/mp4'}">
            Your browser does not support the video tag.
        </{f"{'video' if not audio_only else 'audio'}"}
        
        <div class="video-info">
            <h3>{video_info['title']}</h3>
            <p>
                {video_info['author']} • {video_info['views']:,} views
                {'• ' + f"{video_info['length'] // 60}:{video_info['length'] % 60:02d} duration" if not audio_only else ''}
            </p>
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/plyr/3.7.8/plyr.min.js"></script>
    <script>
        document.addEventListener('DOMContentLoaded', () => {{
            const player = new Plyr('#player', {{
                quality: {{
                    default: '{quality.replace('p', '')}',
                    options: {json.dumps([int(q.replace('p', '')) for q in video_info['available_qualities']])},
                    forced: true
                }},
                controls: [
                    'play-large',
                    'play',
                    'progress',
                    'current-time',
                    'mute',
                    'volume',
                    {'audio_only': "['download']" if audio_only else "['captions', 'download', 'fullscreen']"}
                ],
                tooltips: {{
                    controls: true,
                    seek: true
                }}
            }});
        }});
    </script>
</body>
</html>
        """
        
        return HTMLResponse(content=html_content)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating embed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    print(f"Starting YouTube API server on {settings.HOST}:{settings.PORT}")
    print(f"Cache directory: {settings.CACHE_DIR}")
    print(f"FFmpeg available: {FFMPEG_AVAILABLE}")
    
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
