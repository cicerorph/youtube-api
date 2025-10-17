from fastapi import FastAPI, HTTPException, Query, Depends, Header, Request, WebSocket, WebSocketDisconnect
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
import gc
from enum import Enum
from datetime import datetime, timedelta
import json
import aiohttp
import random
from collections import defaultdict

from config import settings

app = FastAPI(title="YouTube Video Streaming API")

# Multi-server management
class ServerManager:
    def __init__(self):
        self.servers: Dict[str, Dict] = {}  # server_url -> {status, load, last_check}
        self.websockets: Dict[str, WebSocket] = {}  # server_url -> websocket
        self.server_load: Dict[str, int] = defaultdict(int)  # Track current load per server
        
    def add_server(self, server_url: str, websocket: WebSocket = None):
        """Register a worker server"""
        self.servers[server_url] = {
            'status': 'online',
            'load': 0,
            'last_check': time.time()
        }
        if websocket:
            self.websockets[server_url] = websocket
            
    def remove_server(self, server_url: str):
        """Unregister a worker server"""
        if server_url in self.servers:
            del self.servers[server_url]
        if server_url in self.websockets:
            del self.websockets[server_url]
        if server_url in self.server_load:
            del self.server_load[server_url]
            
    def get_least_loaded_server(self) -> Optional[str]:
        """Get the server with the least load"""
        if not self.servers:
            return None
        online_servers = {url: data for url, data in self.servers.items() if data['status'] == 'online'}
        if not online_servers:
            return None
        return min(online_servers.keys(), key=lambda url: self.server_load.get(url, 0))
    
    def increment_load(self, server_url: str):
        """Increment the load for a server"""
        self.server_load[server_url] += 1
        if server_url in self.servers:
            self.servers[server_url]['load'] = self.server_load[server_url]
    
    def decrement_load(self, server_url: str):
        """Decrement the load for a server"""
        if self.server_load[server_url] > 0:
            self.server_load[server_url] -= 1
        if server_url in self.servers:
            self.servers[server_url]['load'] = self.server_load[server_url]
            
    async def check_cache_on_servers(self, cache_key: str) -> Optional[str]:
        """Check if any server has the cached file"""
        for server_url in list(self.servers.keys()):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{server_url}/internal/cache/check/{cache_key}",
                        timeout=aiohttp.ClientTimeout(total=5)
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data.get('exists'):
                                return server_url
            except Exception as e:
                print(f"Error checking cache on {server_url}: {str(e)}")
        return None
    
    async def proxy_request_to_server(self, server_url: str, path: str, params: Dict = None):
        """Proxy a request to a worker server"""
        try:
            self.increment_load(server_url)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{server_url}{path}",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=300)
                ) as response:
                    return response
        finally:
            self.decrement_load(server_url)
            
    async def broadcast_to_servers(self, message: dict):
        """Broadcast a message to all connected worker servers via WebSocket"""
        disconnected = []
        for server_url, ws in self.websockets.items():
            try:
                await ws.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to {server_url}: {str(e)}")
                disconnected.append(server_url)
        
        for server_url in disconnected:
            self.remove_server(server_url)

server_manager = ServerManager() if settings.MULTI_SERVER_ENABLED and settings.MULTI_SERVER_MAIN else None

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
        # Bypass rate limit for admin API key
        if settings.ADMIN_API_KEY and request.headers.get("X-API-Key") == settings.ADMIN_API_KEY:
            return await call_next(request)
        
        # Bypass rate limit for /job/ endpoint
        if request.url.path.startswith("/job/"):
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

# Job tracking system
JOBS_FILE = os.path.join(settings.CACHE_DIR, "jobs.json")
download_jobs: Dict[str, Dict] = {}

def load_jobs():
    """Load jobs from disk"""
    global download_jobs
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, 'r') as f:
                download_jobs = json.load(f)
        except Exception as e:
            print(f"Error loading jobs: {str(e)}")
            download_jobs = {}
    else:
        download_jobs = {}

def save_jobs():
    """Save jobs to disk"""
    try:
        with open(JOBS_FILE, 'w') as f:
            json.dump(download_jobs, f, indent=2)
    except Exception as e:
        print(f"Error saving jobs: {str(e)}")

def clean_expired_jobs():
    """Remove jobs for expired cache files"""
    global download_jobs
    cleaned_count = 0
    jobs_to_remove = []
    
    for job_key, job_data in download_jobs.items():
        if job_data.get('status') == 'completed':
            cache_path = job_data.get('cache_path')
            if cache_path and (not os.path.exists(cache_path) or is_cache_file_expired(cache_path)):
                jobs_to_remove.append(job_key)
                cleaned_count += 1
    
    for job_key in jobs_to_remove:
        del download_jobs[job_key]
    
    if jobs_to_remove:
        save_jobs()
    
    return cleaned_count

# Load existing jobs on startup
load_jobs()

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
        
        jobs_cleaned = clean_expired_jobs()
        print(f"Cleaned {jobs_cleaned} expired jobs")
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
    
    # Force garbage collection to free memory
    gc.collect()
    
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
    
    # Force garbage collection to free memory
    gc.collect()
    
    return audio_output

@app.get("/video/{video_id}")
async def stream_youtube_video(
    request: Request,
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
        
        # Multi-server mode: Check if this is the main server
        if settings.MULTI_SERVER_ENABLED and settings.MULTI_SERVER_MAIN and server_manager:
            # First, check if any worker server has this file cached
            server_with_cache = await server_manager.check_cache_on_servers(cache_key)
            
            if server_with_cache:
                # Proxy the request to the server that has the cache
                print(f"Proxying request to {server_with_cache} (has cache)")
                try:
                    server_manager.increment_load(server_with_cache)
                    async with aiohttp.ClientSession() as session:
                        params = {
                            'quality': quality,
                            'format_type': format_type.value,
                            'audio_only': str(audio_only).lower()
                        }
                        
                        # Get range header from original request
                        headers = {}
                        if request.headers.get("range"):
                            headers["range"] = request.headers.get("range")
                        
                        async with session.get(
                            f"{server_with_cache}/video/{video_id}",
                            params=params,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=300)
                        ) as response:
                            # Stream the response back to client
                            response_headers = dict(response.headers)
                            
                            async def stream_from_server():
                                async for chunk in response.content.iter_chunked(65536):
                                    yield chunk
                            
                            return StreamingResponse(
                                stream_from_server(),
                                status_code=response.status,
                                headers=response_headers,
                                media_type=response.headers.get('content-type', 'video/mp4')
                            )
                finally:
                    server_manager.decrement_load(server_with_cache)
            else:
                # No server has cache, route to least loaded server
                target_server = server_manager.get_least_loaded_server()
                
                if target_server:
                    print(f"Routing request to least loaded server: {target_server}")
                    try:
                        server_manager.increment_load(target_server)
                        async with aiohttp.ClientSession() as session:
                            params = {
                                'quality': quality,
                                'format_type': format_type.value,
                                'audio_only': str(audio_only).lower()
                            }
                            
                            # Get range header from original request
                            headers = {}
                            if request.headers.get("range"):
                                headers["range"] = request.headers.get("range")
                            
                            async with session.get(
                                f"{target_server}/video/{video_id}",
                                params=params,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=300)
                            ) as response:
                                # Stream the response back to client
                                response_headers = dict(response.headers)
                                
                                async def stream_from_server():
                                    async for chunk in response.content.iter_chunked(65536):
                                        yield chunk
                                
                                return StreamingResponse(
                                    stream_from_server(),
                                    status_code=response.status,
                                    headers=response_headers,
                                    media_type=response.headers.get('content-type', 'video/mp4')
                                )
                    finally:
                        server_manager.decrement_load(target_server)
                # If no workers available, fall through to handle locally
        
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
        
        # Get file size for Range header support
        file_size = os.path.getsize(cache_path)
        
        # Parse Range header
        range_header = request.headers.get("range")
        start = 0
        end = file_size - 1
        
        if range_header:
            range_match = range_header.replace("bytes=", "").split("-")
            start = int(range_match[0]) if range_match[0] else 0
            end = int(range_match[1]) if range_match[1] else file_size - 1
            
            if start >= file_size or end >= file_size:
                raise HTTPException(status_code=416, detail="Requested range not satisfiable")
        
        content_length = end - start + 1
        
        def iterfile():
            with open(cache_path, 'rb') as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(1024 * 1024, remaining)
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
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
        
        headers = {
            "Content-Type": mime_type,
            "Content-Disposition": f"inline; filename={video_id}{os.path.splitext(cache_path)[1]}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
        }
        
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            status_code = 206  # Partial Content
        else:
            status_code = 200
        
        return StreamingResponse(
            iterfile(),
            media_type=mime_type,
            status_code=status_code,
            headers=headers
        )
    
    except Exception as e:
        import traceback
        error_detail = str(e)
        print(f"Error in stream_youtube_video: {error_detail}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error retrieving content: {error_detail}")

@app.post("/request/{video_id}")
async def request_video_download(
    video_id: str,
    quality: str = Query(None, description=f"Desired video quality (e.g., '1080p', '720p', '480p', '360p'). Default: {settings.DEFAULT_QUALITY}"),
    format_type: FormatType = Query(FormatType.MP4, description="Video format type"),
    audio_only: bool = Query(False, description="Get audio-only stream")
):
    """Request a video download as a background job"""
    if quality is None:
        quality = settings.DEFAULT_QUALITY
    
    try:
        # Multi-server mode: Route to least loaded server
        if settings.MULTI_SERVER_ENABLED and settings.MULTI_SERVER_MAIN and server_manager:
            # First check if any server has it cached
            cache_key = f"{video_id}_{quality if quality else 'best'}"
            if audio_only:
                cache_key += f"_audio_{format_type}"
            job_key = hashlib.md5(cache_key.encode()).hexdigest()
            
            server_with_cache = await server_manager.check_cache_on_servers(job_key)
            
            if server_with_cache:
                # Return job info pointing to the server with cache
                return {
                    'job_id': job_key,
                    'video_id': video_id,
                    'status': 'completed',
                    'message': 'File already cached on worker server',
                    'server': server_with_cache
                }
            
            # Route to least loaded server
            target_server = server_manager.get_least_loaded_server()
            
            if target_server:
                print(f"Routing download request to: {target_server}")
                try:
                    async with aiohttp.ClientSession() as session:
                        params = {
                            'quality': quality,
                            'format_type': format_type.value,
                            'audio_only': str(audio_only).lower()
                        }
                        
                        async with session.post(
                            f"{target_server}/request/{video_id}",
                            params=params,
                            timeout=aiohttp.ClientTimeout(total=30)
                        ) as response:
                            if response.status == 200:
                                result = await response.json()
                                result['server'] = target_server
                                return result
                            else:
                                error_text = await response.text()
                                print(f"Error from worker server: {error_text}")
                except Exception as e:
                    print(f"Error routing to worker server: {str(e)}")
                    # Fall through to handle locally
        
        # Create job key
        cache_key = f"{video_id}_{quality if quality else 'best'}"
        if audio_only:
            cache_key += f"_audio_{format_type}"
        job_key = hashlib.md5(cache_key.encode()).hexdigest()
        
        file_ext = format_type.value
        cache_path = os.path.join(settings.CACHE_DIR, f"{job_key}.{file_ext}")
        
        # Check if file already exists
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0 and not is_cache_file_expired(cache_path):
            # File already cached
            if job_key not in download_jobs or download_jobs[job_key].get('status') != 'completed':
                download_jobs[job_key] = {
                    'video_id': video_id,
                    'quality': quality,
                    'format_type': format_type.value,
                    'audio_only': audio_only,
                    'status': 'completed',
                    'cache_path': cache_path,
                    'file_size': os.path.getsize(cache_path),
                    'created_at': datetime.now().isoformat(),
                    'completed_at': datetime.now().isoformat(),
                    'message': 'File already cached'
                }
                save_jobs()
            
            return {
                'job_id': job_key,
                'video_id': video_id,
                'status': 'completed',
                'message': 'Video already cached and ready'
            }
        
        # Check if job already exists
        if job_key in download_jobs:
            existing_job = download_jobs[job_key]
            if existing_job.get('status') in ['pending', 'downloading']:
                return {
                    'job_id': job_key,
                    'video_id': video_id,
                    'status': existing_job.get('status'),
                    'message': 'Download already in progress'
                }
        
        # Create new job
        download_jobs[job_key] = {
            'video_id': video_id,
            'quality': quality,
            'format_type': format_type.value,
            'audio_only': audio_only,
            'status': 'pending',
            'cache_path': cache_path,
            'created_at': datetime.now().isoformat(),
            'message': 'Download queued'
        }
        save_jobs()
        
        # Start download in background
        async def download_task():
            try:
                download_jobs[job_key]['status'] = 'downloading'
                download_jobs[job_key]['started_at'] = datetime.now().isoformat()
                save_jobs()
                
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
                                raise Exception(f"Format {format_type} requires yt-dlp which failed. Error: {str(e)}")
                    else:
                        await download_with_pytube(video_id, quality, cache_path)
                
                # Check for alternate file extensions
                if not os.path.exists(cache_path):
                    base_path = os.path.splitext(cache_path)[0]
                    for ext in [f'.{format_type}', '.mp4', '.mkv', '.webm', '.mp4.mkv', '.mp4.webm', '.m4a', '.mp3']:
                        alt_path = f"{base_path}{ext}"
                        if os.path.exists(alt_path):
                            cache_path_final = alt_path
                            download_jobs[job_key]['cache_path'] = cache_path_final
                            break
                    else:
                        raise FileNotFoundError(f"Could not find downloaded file for {video_id}")
                else:
                    cache_path_final = cache_path
                
                download_jobs[job_key]['status'] = 'completed'
                download_jobs[job_key]['completed_at'] = datetime.now().isoformat()
                download_jobs[job_key]['file_size'] = os.path.getsize(cache_path_final)
                download_jobs[job_key]['message'] = 'Download completed successfully'
                save_jobs()
                
            except Exception as e:
                download_jobs[job_key]['status'] = 'failed'
                download_jobs[job_key]['error'] = str(e)
                download_jobs[job_key]['completed_at'] = datetime.now().isoformat()
                download_jobs[job_key]['message'] = f'Download failed: {str(e)}'
                save_jobs()
        
        # Run download in background
        asyncio.create_task(download_task())
        
        return {
            'job_id': job_key,
            'video_id': video_id,
            'status': 'pending',
            'message': 'Download started'
        }
        
    except Exception as e:
        import traceback
        error_detail = str(e)
        print(f"Error in request_video_download: {error_detail}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error creating download request: {error_detail}")

@app.get("/job/{job_id}")
async def get_job_status(job_id: str):
    """Get the status of a download job"""
    if job_id not in download_jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job_data = download_jobs[job_id]
    
    response = {
        'job_id': job_id,
        'video_id': job_data.get('video_id'),
        'quality': job_data.get('quality'),
        'format_type': job_data.get('format_type'),
        'audio_only': job_data.get('audio_only'),
        'status': job_data.get('status'),
        'message': job_data.get('message'),
        'created_at': job_data.get('created_at'),
    }
    
    # Add optional fields based on status
    if job_data.get('started_at'):
        response['started_at'] = job_data.get('started_at')
    
    if job_data.get('completed_at'):
        response['completed_at'] = job_data.get('completed_at')
    
    if job_data.get('file_size'):
        response['file_size'] = job_data.get('file_size')
        response['file_size_mb'] = round(job_data.get('file_size') / (1024 * 1024), 2)
    
    if job_data.get('error'):
        response['error'] = job_data.get('error')
    
    # If completed, provide download URL
    if job_data.get('status') == 'completed':
        cache_path = job_data.get('cache_path')
        if cache_path and os.path.exists(cache_path):
            response['download_url'] = f"/video/{job_data.get('video_id')}?quality={job_data.get('quality')}"
            if job_data.get('audio_only'):
                response['download_url'] += f"&audio_only=true&format_type={job_data.get('format_type')}"
            
            # Check if file is expired
            if is_cache_file_expired(cache_path):
                response['cache_expired'] = True
                response['message'] = 'Cache file has expired'
        else:
            response['cache_missing'] = True
            response['message'] = 'Cache file not found'
    
    return response

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
    
    loop = asyncio.get_event_loop()
    
    if high_quality_requested:
        video_stream = yt.streams.filter(
            adaptive=True, 
            file_extension='mp4', 
            resolution=quality,
            type="video"
        ).first()
        
        if not video_stream:
            video_stream = progressive_streams.order_by('resolution').last()
            # Use download() instead of stream_to_buffer to avoid memory bloat
            await loop.run_in_executor(None, lambda: video_stream.download(output_path=settings.CACHE_DIR, filename=os.path.basename(output_path)))
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
            
            # Download directly to files instead of buffering in memory
            await loop.run_in_executor(None, lambda: video_stream.download(output_path=settings.CACHE_DIR, filename=os.path.basename(temp_video)))
            await loop.run_in_executor(None, lambda: audio_stream.download(output_path=settings.CACHE_DIR, filename=os.path.basename(temp_audio)))
            
            await combine_audio_video(temp_video, temp_audio, output_path)
            
            try:
                os.remove(temp_video)
                os.remove(temp_audio)
            except Exception as e:
                print(f"Error removing temp files: {e}")
    else:
        if quality:
            video_stream = progressive_streams.filter(resolution=quality).first()
            if not video_stream:
                video_stream = progressive_streams.order_by('resolution').last()
        else:
            video_stream = progressive_streams.order_by('resolution').last()
        
        if not video_stream:
            raise HTTPException(status_code=404, detail="No suitable video stream found")
        
        # Use download() instead of stream_to_buffer to avoid memory bloat
        await loop.run_in_executor(None, lambda: video_stream.download(output_path=settings.CACHE_DIR, filename=os.path.basename(output_path)))
    
    # Force garbage collection to free memory
    gc.collect()

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
            {'poster="https://corsproxy.io/?url=' + video_info.get('thumbnail_url', '') + '"' if not audio_only else ''}>
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
                    'fullscreen',
                    'download'
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

# ============= Multi-Server Endpoints =============

@app.websocket("/ws/worker")
async def worker_websocket(websocket: WebSocket, server_url: str = Query(..., description="Worker server URL")):
    """WebSocket endpoint for worker servers to connect to main server"""
    if not settings.MULTI_SERVER_ENABLED or not settings.MULTI_SERVER_MAIN:
        await websocket.close(code=1008, reason="Multi-server mode not enabled or not main server")
        return
    
    await websocket.accept()
    server_manager.add_server(server_url, websocket)
    print(f"Worker server connected: {server_url}")
    
    try:
        while True:
            data = await websocket.receive_json()
            # Handle messages from worker servers
            message_type = data.get('type')
            
            if message_type == 'heartbeat':
                server_manager.servers[server_url]['last_check'] = time.time()
                await websocket.send_json({'type': 'heartbeat_ack'})
            elif message_type == 'load_update':
                server_manager.server_load[server_url] = data.get('load', 0)
                server_manager.servers[server_url]['load'] = data.get('load', 0)
            elif message_type == 'status_update':
                server_manager.servers[server_url]['status'] = data.get('status', 'online')
    except WebSocketDisconnect:
        print(f"Worker server disconnected: {server_url}")
        server_manager.remove_server(server_url)
    except Exception as e:
        print(f"WebSocket error with {server_url}: {str(e)}")
        server_manager.remove_server(server_url)

@app.get("/internal/cache/check/{cache_key}")
async def check_cache_exists(cache_key: str):
    """Internal endpoint to check if a cache file exists on this server"""
    # Check for different file extensions
    extensions = ['mp4', 'mkv', 'webm', 'mp3', 'm4a']
    
    for ext in extensions:
        cache_path = os.path.join(settings.CACHE_DIR, f"{cache_key}.{ext}")
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0 and not is_cache_file_expired(cache_path):
            return {
                'exists': True,
                'cache_key': cache_key,
                'file_size': os.path.getsize(cache_path),
                'extension': ext
            }
    
    return {'exists': False, 'cache_key': cache_key}

@app.get("/admin/servers", dependencies=[Depends(verify_admin_api_key)])
async def get_server_status():
    """Get status of all worker servers (main server only)"""
    if not settings.MULTI_SERVER_ENABLED or not settings.MULTI_SERVER_MAIN:
        raise HTTPException(status_code=400, detail="Not a main server")
    
    if not server_manager:
        raise HTTPException(status_code=400, detail="Server manager not initialized")
    
    return {
        'servers': server_manager.servers,
        'total_servers': len(server_manager.servers),
        'online_servers': len([s for s in server_manager.servers.values() if s['status'] == 'online'])
    }

if __name__ == "__main__":
    import uvicorn
    print(f"Starting YouTube API server on {settings.HOST}:{settings.PORT}")
    print(f"Cache directory: {settings.CACHE_DIR}")
    print(f"FFmpeg available: {FFMPEG_AVAILABLE}")
    
    if settings.MULTI_SERVER_ENABLED:
        if settings.MULTI_SERVER_MAIN:
            print(f"Multi-server mode: MAIN SERVER")
            print(f"Configured worker servers: {len(settings.MULTI_SERVER_URLS)}")
        else:
            print(f"Multi-server mode: WORKER SERVER")
    
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
