#!/usr/bin/env python3
"""
Worker Server Connector

This script connects a worker server to the main server via WebSocket
to enable multi-server functionality. Run this alongside your worker server.
"""

import asyncio
import websockets
import json
import os
import sys
import time
import aiohttp
from datetime import datetime

# Configuration
MAIN_SERVER_URL = os.getenv("MAIN_SERVER_URL", "ws://localhost:8000")
WORKER_SERVER_URL = os.getenv("WORKER_SERVER_URL", "http://localhost:8001")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))  # seconds

class WorkerConnector:
    def __init__(self, main_server_url: str, worker_server_url: str):
        self.main_server_url = main_server_url
        self.worker_server_url = worker_server_url
        self.websocket = None
        self.running = False
        
    async def connect(self):
        """Connect to the main server via WebSocket"""
        ws_url = f"{self.main_server_url}/ws/worker?server_url={self.worker_server_url}"
        
        print(f"[{datetime.now()}] Connecting to main server: {ws_url}")
        
        try:
            async with websockets.connect(ws_url) as websocket:
                self.websocket = websocket
                self.running = True
                print(f"[{datetime.now()}] ✓ Connected to main server")
                
                # Start heartbeat task
                heartbeat_task = asyncio.create_task(self.send_heartbeat())
                
                # Listen for messages from main server
                try:
                    async for message in websocket:
                        await self.handle_message(json.loads(message))
                except websockets.exceptions.ConnectionClosed:
                    print(f"[{datetime.now()}] Connection closed by server")
                finally:
                    self.running = False
                    heartbeat_task.cancel()
                    
        except Exception as e:
            print(f"[{datetime.now()}] ✗ Connection error: {str(e)}")
            self.running = False
    
    async def handle_message(self, message: dict):
        """Handle messages from the main server"""
        msg_type = message.get('type')
        
        if msg_type == 'heartbeat_ack':
            print(f"[{datetime.now()}] ♥ Heartbeat acknowledged")
        else:
            print(f"[{datetime.now()}] Received message: {message}")
    
    async def send_heartbeat(self):
        """Send periodic heartbeat to main server"""
        while self.running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                
                if self.websocket and not self.websocket.closed:
                    # Get current server load (you can customize this)
                    load = await self.get_current_load()
                    
                    await self.websocket.send(json.dumps({
                        'type': 'heartbeat',
                        'timestamp': datetime.now().isoformat(),
                        'load': load
                    }))
                    
                    # Also send load update
                    await self.websocket.send(json.dumps({
                        'type': 'load_update',
                        'load': load
                    }))
                    
            except Exception as e:
                print(f"[{datetime.now()}] Heartbeat error: {str(e)}")
                break
    
    async def get_current_load(self) -> int:
        """Get current server load (customize based on your metrics)"""
        # This is a simple implementation - you can enhance it
        # to get actual CPU/memory usage or request count
        return 0
    
    async def run(self):
        """Main run loop with auto-reconnect"""
        retry_delay = 5
        
        while True:
            try:
                await self.connect()
            except KeyboardInterrupt:
                print(f"\n[{datetime.now()}] Shutting down...")
                break
            except Exception as e:
                print(f"[{datetime.now()}] Error: {str(e)}")
            
            print(f"[{datetime.now()}] Reconnecting in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
            
            # Exponential backoff up to 60 seconds
            retry_delay = min(retry_delay * 2, 60)

async def main():
    if len(sys.argv) > 1:
        main_server = sys.argv[1]
    else:
        main_server = MAIN_SERVER_URL
    
    if len(sys.argv) > 2:
        worker_server = sys.argv[2]
    else:
        worker_server = WORKER_SERVER_URL
    
    print("=" * 60)
    print("Worker Server Connector")
    print("=" * 60)
    print(f"Main Server:   {main_server}")
    print(f"Worker Server: {worker_server}")
    print(f"Heartbeat:     Every {HEARTBEAT_INTERVAL} seconds")
    print("=" * 60)
    print()
    
    connector = WorkerConnector(main_server, worker_server)
    await connector.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete")
