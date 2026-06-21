#!/usr/bin/env python3
"""
Anime CLI Player — Stream & Play
High-performance terminal client:
  * Interactive CLI with Rich TUI components
  * Concurrent async scraping using asyncio and Playwright
  * Autoplay bypass: resolves player embed URLs directly in top-level context
  * Automatic VLC/MPV discovery (PATH, Registry, default directories)
  * Automatic player installation if missing (winget, choco, apt, dnf, pacman)
  * Detached player launch (no CLI lock)
  * Cross-platform: Windows and Linux
"""

# ── Check and Install Missing Python Dependencies ──
def check_and_install_dependencies():
    required_packages = {
        "requests": "requests",
        "bs4": "beautifulsoup4",
        "rich": "rich",
        "playwright": "playwright",
        "httpx": "httpx",
    }
    
    crypto_installed = False
    try:
        from Cryptodome.Cipher import AES
        crypto_installed = True
    except ImportError:
        try:
            from Crypto.Cipher import AES
            crypto_installed = True
        except ImportError:
            pass
            
    missing_packages = []
    for imp_name, pip_name in required_packages.items():
        try:
            __import__(imp_name)
        except ImportError:
            missing_packages.append(pip_name)
            
    if not crypto_installed:
        missing_packages.append("pycryptodome")
        
    if missing_packages:
        print("Missing required libraries: " + ", ".join(missing_packages))
        print("Attempting to install them automatically...")
        import sys
        import subprocess
        
        pip_cmd = [sys.executable, "-m", "pip", "install"] + missing_packages
        installed_ok = False
        try:
            subprocess.run(pip_cmd, check=True)
            installed_ok = True
        except Exception:
            try:
                print("Retrying with bypass flags (--user --break-system-packages)...")
                fallback_cmd = [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages"] + missing_packages
                subprocess.run(fallback_cmd, check=True)
                installed_ok = True
            except Exception as e:
                print(f"Error installing dependencies: {e}")
                print("Please install them manually using: pip install " + " ".join(missing_packages))
                sys.exit(1)
                
        if installed_ok:
            print("Successfully installed missing libraries!")
            if "playwright" in missing_packages:
                print("Installing Playwright Chromium browser binaries...")
                try:
                    playwright_install_cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
                    subprocess.run(playwright_install_cmd, check=True)
                    print("Playwright Chromium browser installed successfully!")
                except Exception as e:
                    print(f"Warning: Playwright browser installation failed: {e}")
                    print("You may need to run 'playwright install' manually later.")

check_and_install_dependencies()

import asyncio
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse, quote_plus

import httpx
import requests
from bs4 import BeautifulSoup

try:
    from Cryptodome.Cipher import AES
except ImportError:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        AES = None

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.columns import Columns
from rich import box as rich_box


# Conditional platform-specific imports
if os.name == 'nt':
    try:
        import win32crypt
    except ImportError:
        win32crypt = None
    import msvcrt
else:
    import tty
    import termios
    import select
    win32crypt = None
    msvcrt = None

console = Console()

# ════════════════════════════════════════════════════════════
#  Local Configuration Management
# ════════════════════════════════════════════════════════════

def get_config_dir():
    home = os.path.expanduser("~")
    if os.name == 'nt':
        appdata = os.environ.get("APPDATA")
        if appdata:
            path = os.path.join(appdata, "pyanime")
        else:
            path = os.path.join(home, ".config", "pyanime")
    else:
        path = os.path.join(home, ".config", "pyanime")
    os.makedirs(path, exist_ok=True)
    return path

def get_db_path():
    return os.path.join(get_config_dir(), "pyanime.db")

def init_db():
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                slug TEXT PRIMARY KEY,
                title TEXT,
                url TEXT,
                is_witanime INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shows (
                slug TEXT PRIMARY KEY,
                last_watched INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watched_episodes (
                slug TEXT,
                episode INTEGER,
                PRIMARY KEY (slug, episode)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS episode_progress (
                slug TEXT,
                episode INTEGER,
                time_pos REAL,
                duration REAL,
                PRIMARY KEY (slug, episode)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                platform TEXT PRIMARY KEY,
                token TEXT,
                client_id TEXT,
                refresh_token TEXT,
                expires_at REAL
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

def migrate_json_to_sqlite():
    p = get_config_path()
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        
        migrated = False
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        if "favorites" in cfg and cfg["favorites"]:
            for fav in cfg["favorites"]:
                slug = fav.get("slug")
                title = fav.get("title")
                url = fav.get("url")
                is_witanime = int(fav.get("is_witanime", 0))
                if slug:
                    cursor.execute(
                        "INSERT OR IGNORE INTO favorites (slug, title, url, is_witanime) VALUES (?, ?, ?, ?)",
                        (slug, title, url, is_witanime)
                    )
            del cfg["favorites"]
            migrated = True

        if "history" in cfg and cfg["history"]:
            for slug, hist_item in cfg["history"].items():
                last_watched = hist_item.get("last_watched", 0)
                cursor.execute(
                    "INSERT OR REPLACE INTO shows (slug, last_watched) VALUES (?, ?)",
                    (slug, last_watched)
                )
                watched = hist_item.get("watched", [])
                for ep in watched:
                    cursor.execute(
                        "INSERT OR IGNORE INTO watched_episodes (slug, episode) VALUES (?, ?)",
                        (slug, ep)
                    )
            del cfg["history"]
            migrated = True

        if migrated:
            conn.commit()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
        conn.close()
    except Exception:
        pass

def save_episode_progress(slug, ep, time_pos, duration):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO episode_progress (slug, episode, time_pos, duration) VALUES (?, ?, ?, ?)",
            (slug, ep, time_pos, duration)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_episode_progress(slug, ep):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT time_pos, duration FROM episode_progress WHERE slug = ? AND episode = ?",
            (slug, ep)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"time_pos": row[0], "duration": row[1]}
    except Exception:
        pass
    return None

def poll_mpv_progress(ipc_path, slug, ep):
    client = None
    for _ in range(20):
        if os.name == 'nt':
            try:
                client = open(ipc_path, "r+b", buffering=0)
                break
            except Exception:
                time.sleep(0.5)
        else:
            if os.path.exists(ipc_path):
                try:
                    import socket
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(ipc_path)
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                time.sleep(0.5)

    if not client:
        return

    try:
        while True:
            time_cmd = json.dumps({"command": ["get_property", "time-pos"]}) + "\n"
            duration_cmd = json.dumps({"command": ["get_property", "duration"]}) + "\n"

            time_pos = None
            duration = None

            if os.name == 'nt':
                try:
                    client.write(time_cmd.encode("utf-8"))
                    line = b""
                    while True:
                        c = client.read(1)
                        if not c:
                            break
                        line += c
                        if c == b"\n":
                            break
                    if line:
                        resp = json.loads(line.decode("utf-8", errors="ignore"))
                        if resp.get("error") == "success":
                            time_pos = resp.get("data")
                except Exception:
                    break

                try:
                    client.write(duration_cmd.encode("utf-8"))
                    line = b""
                    while True:
                        c = client.read(1)
                        if not c:
                            break
                        line += c
                        if c == b"\n":
                            break
                    if line:
                        resp = json.loads(line.decode("utf-8", errors="ignore"))
                        if resp.get("error") == "success":
                            duration = resp.get("data")
                except Exception:
                    break
            else:
                try:
                    client.sendall(time_cmd.encode("utf-8"))
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                    if b"\n" in buffer:
                        line = buffer.split(b"\n")[0]
                        resp = json.loads(line.decode("utf-8", errors="ignore"))
                        if resp.get("error") == "success":
                            time_pos = resp.get("data")
                except Exception:
                    break

                try:
                    client.sendall(duration_cmd.encode("utf-8"))
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                    if b"\n" in buffer:
                        line = buffer.split(b"\n")[0]
                        resp = json.loads(line.decode("utf-8", errors="ignore"))
                        if resp.get("error") == "success":
                            duration = resp.get("data")
                except Exception:
                    break

            if time_pos is not None:
                if duration and (time_pos / duration > 0.95):
                    save_episode_progress(slug, ep, 0, duration)
                else:
                    save_episode_progress(slug, ep, time_pos, duration or 0)

            time.sleep(2.0)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        if os.name != 'nt':
            try:
                if os.path.exists(ipc_path):
                    os.remove(ipc_path)
            except Exception:
                pass

def get_config_path():
    return os.path.join(get_config_dir(), "config.json")

def load_config():
    p = get_config_path()
    default_cfg = {
        "preferred_player": "auto",
        "default_quality": "auto",
        "preferred_browser": "auto",
        "history_tracking": True,
        "fullscreen": True,
        "custom_player_args": "",
        "nerd_fonts": False,
        "search_history": [],
        "favorites": [],
        "history": {}
    }
    if not os.path.exists(p):
        return default_cfg
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in default_cfg.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except Exception:
        return default_cfg

def save_config(cfg):
    p = get_config_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def add_search_history(query):
    cfg = load_config()
    hist = cfg.get("search_history", [])
    if query in hist:
        hist.remove(query)
    hist.insert(0, query)
    cfg["search_history"] = hist[:5]
    save_config(cfg)

def toggle_favorite_state(title, url, is_witanime, slug):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM favorites WHERE slug = ?", (slug,))
        exists = cursor.fetchone() is not None
        if exists:
            cursor.execute("DELETE FROM favorites WHERE slug = ?", (slug,))
            ret = False
        else:
            cursor.execute(
                "INSERT INTO favorites (slug, title, url, is_witanime) VALUES (?, ?, ?, ?)",
                (slug, title, url, int(is_witanime))
            )
            ret = True
        conn.commit()
        conn.close()
        return ret
    except Exception:
        return False

def is_favorite_slug(slug):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM favorites WHERE slug = ?", (slug,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception:
        return False

def add_watch_history(slug, episode_num, anime_title=None):
    cfg = load_config()
    if not cfg.get("history_tracking", True):
        return
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO shows (slug, last_watched) VALUES (?, ?)", (slug, episode_num))
        cursor.execute("INSERT OR IGNORE INTO watched_episodes (slug, episode) VALUES (?, ?)", (slug, episode_num))
        conn.commit()
        conn.close()
    except Exception:
        pass
    
    # Sync with external platforms in background
    sync_watch_progress_bg(slug, anime_title, episode_num)


def get_watch_history(slug):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT last_watched FROM shows WHERE slug = ?", (slug,))
        row = cursor.fetchone()
        last_watched = row[0] if row else 0

        cursor.execute("SELECT episode FROM watched_episodes WHERE slug = ?", (slug,))
        watched = [r[0] for r in cursor.fetchall()]
        conn.close()
        return {"last_watched": last_watched, "watched": watched}
    except Exception:
        return {"last_watched": 0, "watched": []}


def save_account_token(platform, token, client_id=None, refresh_token=None, expires_in=None):
    db_path = get_db_path()
    expires_at = time.time() + expires_in if expires_in else None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO accounts (platform, token, client_id, refresh_token, expires_at) VALUES (?, ?, ?, ?, ?)",
            (platform, token, client_id, refresh_token, expires_at)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def get_account_token(platform):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT token, client_id, refresh_token, expires_at FROM accounts WHERE platform = ?", (platform,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "token": row[0],
                "client_id": row[1],
                "refresh_token": row[2],
                "expires_at": row[3]
            }
    except Exception:
        pass
    return None


def remove_account(platform):
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE platform = ?", (platform,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def refresh_mal_token(client_id, refresh_token):
    token_url = "https://myanimelist.net/v1/oauth2/token"
    payload = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    try:
        response = requests.post(token_url, data=payload, timeout=10)
        if response.status_code == 200:
            data = response.json()
            access_token = data["access_token"]
            new_refresh = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 2419200) # Default 28 days
            save_account_token("myanimelist", access_token, client_id, new_refresh, expires_in)
            return access_token
    except Exception:
        pass
    return None


def clean_slug_for_search(slug):
    cleaned = slug.lower()
    cleaned = re.sub(r'-(?:sub|dub|arabic|season|ep|episode|uncut|tv).*$', '', cleaned)
    cleaned = cleaned.replace('-', ' ').strip()
    return cleaned


def sync_to_anilist(token, anime_title, episode_num):
    url = "https://graphql.anilist.co"
    query = """
    query ($search: String) {
      Media (search: $search, type: ANIME) {
        id
        title {
          romaji
          english
          native
        }
      }
    }
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    try:
        response = requests.post(url, json={"query": query, "variables": {"search": anime_title}}, headers=headers, timeout=10)
        if response.status_code != 200:
            return False, f"HTTP Error {response.status_code}"
        data = response.json()
        media = data.get("data", {}).get("Media")
        if not media:
            return False, "Anime not found on AniList"
        media_id = media["id"]
        
        mutation = """
        mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
          SaveMediaListEntry (mediaId: $mediaId, progress: $progress, status: $status) {
            id
            progress
            status
          }
        }
        """
        variables = {
            "mediaId": media_id,
            "progress": episode_num,
            "status": "CURRENT"
        }
        resp2 = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=10)
        if resp2.status_code != 200:
            return False, f"Mutation Error {resp2.status_code}"
        return True, "Success"
    except Exception as e:
        return False, str(e)


def sync_to_myanimelist(token, anime_title, episode_num):
    headers = {
        "Authorization": f"Bearer {token}"
    }
    try:
        search_url = "https://api.myanimelist.net/v2/anime"
        params = {"q": anime_title, "limit": 1}
        response = requests.get(search_url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            return False, f"Search HTTP Error {response.status_code}"
        data = response.json()
        nodes = data.get("data", [])
        if not nodes:
            return False, "Anime not found on MyAnimeList"
        anime_id = nodes[0]["node"]["id"]
        
        update_url = f"https://api.myanimelist.net/v2/anime/{anime_id}/my_list_status"
        payload = {
            "status": "watching",
            "num_watched_episodes": episode_num
        }
        resp2 = requests.put(update_url, data=payload, headers=headers, timeout=10)
        if resp2.status_code != 200:
            return False, f"Update HTTP Error {resp2.status_code}"
        return True, "Success"
    except Exception as e:
        return False, str(e)


def sync_watch_progress_bg(slug, anime_title, episode_num):
    import threading
    def worker():
        # AniList Sync
        anilist_acc = get_account_token("anilist")
        if anilist_acc:
            token = anilist_acc["token"]
            search_term = clean_slug_for_search(slug)
            success, msg = sync_to_anilist(token, search_term, episode_num)
            if not success and anime_title:
                sync_to_anilist(token, anime_title, episode_num)
        
        # MyAnimeList Sync
        mal_acc = get_account_token("myanimelist")
        if mal_acc:
            token = mal_acc["token"]
            client_id = mal_acc["client_id"]
            refresh_token = mal_acc["refresh_token"]
            expires_at = mal_acc["expires_at"]
            
            if expires_at and time.time() > expires_at:
                if client_id and refresh_token:
                    new_token = refresh_mal_token(client_id, refresh_token)
                    if new_token:
                        token = new_token
            
            search_term = clean_slug_for_search(slug)
            success, msg = sync_to_myanimelist(token, search_term, episode_num)
            if not success and anime_title:
                sync_to_myanimelist(token, anime_title, episode_num)
                
    threading.Thread(target=worker, daemon=True).start()


def link_anilist_flow():
    clear_screen()
    print_logo()
    console.print(f"[bold {THEME['primary']}]🔗 Link AniList Account[/bold {THEME['primary']}]")
    console.print(f"[{THEME['fg']}]To link your AniList account, follow these steps:[/{THEME['fg']}]\n")
    auth_url = "https://anilist.co/api/v2/oauth/authorize?client_id=14963&response_type=token"
    console.print(f"1. Open this URL in your browser:\n   [bold {THEME['accent']}]{auth_url}[/bold {THEME['accent']}]\n")
    console.print("2. Authorize the application.")
    console.print("3. Copy the URL of the page you are redirected to, or copy the access token directly.")
    token_input = prompt_input(f"\n❯ Paste the redirected URL or the token here: ")
    if not token_input:
        print_warn("Linking cancelled. Press any key...")
        read_key()
        return
    
    token = token_input
    token_match = re.search(r'access_token=([^&]+)', token_input)
    if token_match:
        token = token_match.group(1)
    
    token = token.strip()
    
    with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Verifying AniList token...[/bold {THEME['primary']}]", spinner="dots"):
        url = "https://graphql.anilist.co"
        query = """
        query {
          Viewer {
            name
          }
        }
        """
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        success = False
        username = None
        error_msg = ""
        try:
            resp = requests.post(url, json={"query": query}, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("data", {}).get("Viewer", {}).get("name")
                if username:
                    success = True
                else:
                    error_msg = "Invalid token response"
            else:
                error_msg = f"HTTP {resp.status_code}"
        except Exception as e:
            error_msg = str(e)
            
    if success:
        save_account_token("anilist", token)
        print_ok(f"Successfully linked AniList account: [bold]{username}[/bold]!")
    else:
        print_fail(f"Verification failed: {error_msg}")
    
    console.print(f"[{THEME['dim']}]Press any key to continue...[/{THEME['dim']}]")
    read_key()


def link_myanimelist_flow():
    clear_screen()
    print_logo()
    console.print(f"[bold {THEME['primary']}]🔗 Link MyAnimeList Account[/bold {THEME['primary']}]")
    console.print(f"[{THEME['fg']}]To link your MyAnimeList account, you will need a Client ID.[/{THEME['fg']}]")
    console.print(f"[{THEME['dim']}]You can create a Client ID at https://myanimelist.net/apiconfig[/{THEME['dim']}]")
    console.print(f"[{THEME['dim']}]Set the redirect URI to: http://localhost or https://myanimelist.net/[/{THEME['dim']}]\n")
    
    client_id = prompt_input("❯ Enter your MyAnimeList Client ID: ")
    if not client_id:
        print_warn("Linking cancelled. Press any key...")
        read_key()
        return
        
    import random
    import string
    code_verifier = "".join(random.choices(string.ascii_letters + string.digits + "-._~", k=64))
    
    auth_url = f"https://myanimelist.net/v1/oauth2/authorize?response_type=code&client_id={client_id}&code_challenge={code_verifier}&code_challenge_method=plain"
    
    console.print(f"\n1. Open this URL in your browser:\n   [bold {THEME['accent']}]{auth_url}[/bold {THEME['accent']}]\n")
    console.print("2. Authorize the application.")
    console.print("3. You will be redirected. Copy the full redirected URL (contains '?code=...') and paste it here.")
    
    code_input = prompt_input(f"\n❯ Paste the redirected URL or code here: ")
    if not code_input:
        print_warn("Linking cancelled. Press any key...")
        read_key()
        return
        
    code = code_input.strip()
    code_match = re.search(r'code=([^&]+)', code_input)
    if code_match:
        code = code_match.group(1)
        
    with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Exchanging code for token...[/bold {THEME['primary']}]", spinner="dots"):
        token_url = "https://myanimelist.net/v1/oauth2/token"
        payload = {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier
        }
        success = False
        username = None
        error_msg = ""
        try:
            resp = requests.post(token_url, data=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                access_token = data["access_token"]
                refresh_token = data.get("refresh_token")
                expires_in = data.get("expires_in", 2419200)
                
                user_resp = requests.get("https://api.myanimelist.net/v2/users/@me", headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
                if user_resp.status_code == 200:
                    username = user_resp.json().get("name")
                    save_account_token("myanimelist", access_token, client_id, refresh_token, expires_in)
                    success = True
                else:
                    error_msg = f"User verification failed: HTTP {user_resp.status_code}"
            else:
                error_msg = f"Token exchange failed: HTTP {resp.status_code} - {resp.text}"
        except Exception as e:
            error_msg = str(e)
            
    if success:
        print_ok(f"Successfully linked MyAnimeList account: [bold]{username}[/bold]!")
    else:
        print_fail(f"Link failed: {error_msg}")
        
    console.print(f"[{THEME['dim']}]Press any key to continue...[/{THEME['dim']}]")
    read_key()


# ════════════════════════════════════════════════════════════
#  Color Palette (Premium Dark Mode) & Icons
# ════════════════════════════════════════════════════════════

THEME = {
    "fg": "#E2E8F0",          # Slate-200 (Main text)
    "dim": "#7C8BA1",         # Muted slate (Subtitles)
    "border": "#6C63FF",      # Vibrant Indigo (Borders)
    "primary": "#A78BFA",     # Lavender-400 (Primary accent)
    "accent": "#C084FC",      # Purple-400 (Secondary accent)
    "success": "#4ADE80",     # Emerald-400 (Success / Green)
    "warning": "#FBBF24",     # Amber-400 (Warning / Yellow)
    "error": "#FB7185",       # Rose-400 (Error / Red)
    "select_bg": "#312E81",   # Indigo-900 (Highlight background)
    "select_fg": "#E0E7FF",   # Indigo-100 (Highlight foreground)
    "checked": "#4ADE80",     # Emerald-400
    "unchecked": "#475569",   # Slate-600
    "gradient1": "#C084FC",
    "gradient2": "#818CF8",
    "gradient3": "#6366F1",
    "gradient4": "#4F46E5",
    "gradient5": "#4338CA",
    "separator": "#1E293B",
}

def get_icon(name):
    try:
        cfg = load_config()
        use_nerd = cfg.get("nerd_fonts", False)
    except Exception:
        use_nerd = False

    # Nerd Font unicode values
    nerd_icons = {
        "search": " ",
        "favorite_on": " ",
        "favorite_off": " ",
        "direct_url": " ",
        "settings": " ",
        "exit": " ",
        "play": " ",
        "watch_history": " ",
        "check": " ",
        "cross": " ",
        "warning": " ",
        "info": " ",
        "bullet": " ",
        "arrow_up": " ",
        "arrow_down": " ",
        "folder": " ",
        "sparkle": "✦ "
    }

    # Standard Unicode monochrome icons (highly compatible)
    unicode_icons = {
        "search": "⚲ ",
        "favorite_on": "★ ",
        "favorite_off": "☆ ",
        "direct_url": "🔗 ",
        "settings": "⚙ ",
        "exit": "⏻ ",
        "play": "▶ ",
        "watch_history": "⏳ ",
        "check": "✔ ",
        "cross": "✘ ",
        "warning": "⚠ ",
        "info": "ℹ ",
        "bullet": "❯ ",
        "arrow_up": "▲ ",
        "arrow_down": "▼ ",
        "folder": "📁 ",
        "sparkle": "✦ "
    }

    return nerd_icons[name] if use_nerd else unicode_icons[name]

def get_provider_name(val):
    val = int(val)
    if val == 1:
        return "WitAnime"
    else:
        return "Anime3rb"

def print_info(msg):
    console.print(f"[bold {THEME['primary']}]" + get_icon("info") + f"[/bold {THEME['primary']}] [#E2E8F0]{msg}[/#E2E8F0]")

def print_ok(msg):
    console.print(f"[bold {THEME['success']}]" + get_icon("check") + f"[/bold {THEME['success']}] [#E2E8F0]{msg}[/#E2E8F0]")

def print_warn(msg):
    console.print(f"[bold {THEME['warning']}]" + get_icon("warning") + f"[/bold {THEME['warning']}] [#E2E8F0]{msg}[/#E2E8F0]")

def print_fail(msg):
    console.print(f"[bold {THEME['error']}]" + get_icon("cross") + f"[/bold {THEME['error']}] [#E2E8F0]{msg}[/#E2E8F0]")


# ════════════════════════════════════════════════════════════
#  Key Constants & Input Handling
# ════════════════════════════════════════════════════════════

KEY_UP = "up"
KEY_DOWN = "down"
KEY_ENTER = "enter"
KEY_SPACE = "space"
KEY_ESC = "esc"
KEY_CTRL_C = "ctrl_c"
KEY_A = "a"
KEY_UNKNOWN = "unknown"

_in_raw_mode = False
_raw_fd = None

class RawModeContext:
    def __enter__(self):
        global _in_raw_mode, _raw_fd
        if os.name != 'nt' and sys.stdin.isatty():
            try:
                self.fd = sys.stdin.fileno()
                self.old_settings = termios.tcgetattr(self.fd)
                
                # Custom raw mode that keeps output post-processing (OPOST) enabled
                # to prevent the "staircase effect" (newlines not returning to column 0)
                new_settings = termios.tcgetattr(self.fd)
                new_settings[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
                # Preserve new_settings[1] (OPOST)
                new_settings[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
                new_settings[6][termios.VMIN] = 1
                new_settings[6][termios.VTIME] = 0
                
                termios.tcsetattr(self.fd, termios.TCSADRAIN, new_settings)
                _in_raw_mode = True
                _raw_fd = self.fd
            except Exception:
                self.fd = None
                self.old_settings = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _in_raw_mode, _raw_fd
        if os.name != 'nt' and getattr(self, 'fd', None) is not None and getattr(self, 'old_settings', None) is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass
            _in_raw_mode = False
            _raw_fd = None

def read_key():
    if os.name == 'nt':
        try:
            ch = msvcrt.getch()
            if ch in (b'\x00', b'\xe0'):
                ch += msvcrt.getch()
            if ch == b'\xe0H': return KEY_UP
            if ch == b'\xe0P': return KEY_DOWN
            if ch in (b'\r', b'\n'): return KEY_ENTER
            if ch == b' ': return KEY_SPACE
            if ch == b'\x1b': return KEY_ESC
            if ch == b'\x03': return KEY_CTRL_C
            if ch in (b'a', b'A'): return KEY_A
            return ch.decode('utf-8', errors='ignore')
        except Exception:
            return KEY_UNKNOWN
    else:
        if not sys.stdin.isatty():
            try:
                ch = sys.stdin.read(1)
                if not ch:
                    return KEY_ESC
                if ch in ('\r', '\n'): return KEY_ENTER
                if ch == ' ': return KEY_SPACE
                if ch in ('a', 'A'): return KEY_A
                return ch
            except Exception:
                return KEY_ESC

        if _in_raw_mode and _raw_fd is not None:
            try:
                b = os.read(_raw_fd, 1)
                if not b:
                    return KEY_ESC
                if b == b'\x1b':
                    r, _, _ = select.select([_raw_fd], [], [], 0.05)
                    if r:
                        extra = os.read(_raw_fd, 2)
                        if extra == b'[A': return KEY_UP
                        if extra == b'[B': return KEY_DOWN
                    return KEY_ESC
                if b in (b'\r', b'\n'): return KEY_ENTER
                if b == b' ': return KEY_SPACE
                if b == b'\x03': return KEY_CTRL_C
                if b in (b'a', b'A'): return KEY_A
                return b.decode('utf-8', errors='ignore')
            except Exception:
                return KEY_UNKNOWN
        else:
            try:
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
            except Exception:
                try:
                    ch = sys.stdin.read(1)
                    if not ch: return KEY_ESC
                    return ch
                except Exception:
                    return KEY_UNKNOWN

            try:
                new_settings = termios.tcgetattr(fd)
                new_settings[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
                new_settings[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
                new_settings[6][termios.VMIN] = 1
                new_settings[6][termios.VTIME] = 0
                termios.tcsetattr(fd, termios.TCSADRAIN, new_settings)

                r, _, _ = select.select([fd], [], [])
                if not r:
                    return KEY_UNKNOWN
                b = os.read(fd, 1)
                if not b:
                    return KEY_ESC
                if b == b'\x1b':
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if r:
                        extra = os.read(fd, 2)
                        if extra == b'[A': return KEY_UP
                        if extra == b'[B': return KEY_DOWN
                    return KEY_ESC
                if b in (b'\r', b'\n'): return KEY_ENTER
                if b == b' ': return KEY_SPACE
                if b == b'\x03': return KEY_CTRL_C
                if b in (b'a', b'A'): return KEY_A
                return b.decode('utf-8', errors='ignore')
            except Exception:
                return KEY_UNKNOWN
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass



def flush_input_buffer():
    if os.name == 'nt':
        try:
            while msvcrt.kbhit():
                msvcrt.getch()
        except Exception:
            pass
    else:
        try:
            import select as sel_mod
            while sel_mod.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.read(1)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  Terminal Control
# ════════════════════════════════════════════════════════════

def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

def enter_alt_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[?1049h")
        sys.stdout.flush()

def exit_alt_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()

def set_terminal_title(title):
    if sys.stdout.isatty():
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()

def prompt_input(prompt_text):
    # Show cursor for input
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
    try:
        val = console.input(prompt_text).strip()
        return val
    except (KeyboardInterrupt, EOFError):
        return None
    finally:
        # Hide cursor again
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()



# ════════════════════════════════════════════════════════════
#  Logo
# ════════════════════════════════════════════════════════════

def print_logo():
    lines = [
        "  █████  ███  ███ ███ ███   ███ ██████  ████████ ███  ",
        " ███ ███ ████ ███ ███ █████████ ██  ██  ███      ███  ",
        " ███████ ███ ████ ███ ███ █ ███ ██████  ███████  ███  ",
        " ███ ███ ███  ███ ███ ███   ███ ███     ███           ",
        " ███ ███ ███  ███ ███ ███   ███ ██████  ████████ ███  ",
    ]

    styled_logo = Text()
    colors = [THEME['gradient1'], THEME['gradient2'], THEME['gradient3'], THEME['gradient4'], THEME['gradient5']]
    for idx, line in enumerate(lines):
        color = colors[idx] if idx < len(colors) else THEME['gradient5']
        styled_logo.append(line + "\n", style=f"bold {color}")

    styled_logo.append(f"\n       {get_icon('sparkle')}ANIME STREAMING TERMINAL CLI {get_icon('sparkle')}\n", style=f"bold {THEME['fg']}")
    styled_logo.append(f"            ━━━ v2.0 ━━━\n", style=f"{THEME['dim']}")

    panel = Panel(
        styled_logo,
        border_style=THEME['border'],
        padding=(1, 4),
        box=rich_box.DOUBLE,
        expand=False,
    )
    console.print(panel)


def print_hotkey_guide(player_name):
    title = f" [bold {THEME['primary']}]{player_name} Keyboard Controls / Shortcuts[/bold {THEME['primary']}] "
    
    table = Table(box=None, show_header=True, header_style=f"bold {THEME['accent']}", pad_edge=False)
    table.add_column("Action", style=f"bold {THEME['fg']}")
    table.add_column("Hotkey / Shortcut", style=f"bold {THEME['success']}")

    if player_name == "MPV":
        table.add_row("Play / Pause", "SPACE")
        table.add_row("Seek Back / Forward (5s)", "LEFT / RIGHT")
        table.add_row("Seek Back / Forward (1m)", "UP / DOWN")
        table.add_row("Adjust Volume", "9 / 0")
        table.add_row("Toggle Fullscreen", "F")
        table.add_row("Exit Player", "Q")
    else: # VLC
        table.add_row("Play / Pause", "SPACE")
        table.add_row("Seek Back / Forward", "ALT + LEFT / RIGHT")
        table.add_row("Adjust Volume", "CTRL + UP / DOWN")
        table.add_row("Toggle Fullscreen", "F")
        table.add_row("Exit Player", "Q / ESC")

    panel = Panel(
        table,
        title=title,
        border_style=THEME['border'],
        box=rich_box.ROUNDED,
        padding=(1, 2),
        expand=False
    )
    console.print(panel)


# ════════════════════════════════════════════════════════════
#  Interactive Widgets
# ════════════════════════════════════════════════════════════

_cached_players = {}
def get_cached_players():
    global _cached_players
    if not _cached_players:
        _cached_players = {
            "vlc": find_vlc(),
            "mpv": find_mpv(),
            "iina": find_iina(),
            "celluloid": find_celluloid(),
            "haruna": find_haruna(),
        }
    return _cached_players

def clear_player_cache():
    global _cached_players
    _cached_players.clear()

def get_context_panel(context_type, selected_idx, options, metadata=None):
    if not metadata:
        metadata = {}

    title_text = "Information"
    
    players = get_cached_players()
    vlc_ok = players.get("vlc") is not None
    mpv_ok = players.get("mpv") is not None
    iina_ok = players.get("iina") is not None
    celluloid_ok = players.get("celluloid") is not None
    haruna_ok = players.get("haruna") is not None

    from rich.markup import escape

    if context_type == "main_menu":
        title_text = "System Status & Info"
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column("Key", style=f"bold {THEME['fg']}")
        table.add_column("Val", style=THEME['success'])
        
        player_status = "MPV" if mpv_ok else ("VLC" if vlc_ok else "None")
        pref_player = metadata.get("pref_player", "auto").upper()
        
        table.add_row(f"{get_icon('play')}Preferred Player", f"{pref_player} (Active: {player_status})")
        table.add_row(f"{get_icon('settings')}Quality", metadata.get("default_quality", "auto").upper())
        table.add_row(f"{get_icon('direct_url')}Cookie Browser", metadata.get("pref_browser", "auto").upper())
        table.add_row(f"{get_icon('favorite_on')}Library Size", f"{metadata.get('favorites_count', 0)} show(s)")
        
        al_linked = "[green]Linked[/green]" if metadata.get("anilist_linked") else "[dim]Not Linked[/dim]"
        mal_linked = "[green]Linked[/green]" if metadata.get("mal_linked") else "[dim]Not Linked[/dim]"
        table.add_row(f"{get_icon('watch_history')}AniList Sync", al_linked)
        table.add_row(f"{get_icon('watch_history')}MyAnimeList Sync", mal_linked)

        desc = ""
        if selected_idx == 0:
            desc = "Search for anime series across multiple streaming sources (WitAnime and Anime3rb) in real-time."
        elif selected_idx == 1:
            desc = "Directly play a WitAnime or Anime3rb anime URL without performing a search."
        elif selected_idx == 2:
            desc = "Browse your bookmarked library of shows, view history, and resume playing."
        elif selected_idx == 3:
            desc = "Configure preferred video players, stream quality, cookie sync browsers, and account integrations."
        elif selected_idx == 4:
            desc = "Close the application and exit back to the shell."

        desc_esc = escape(desc)
        renderables = [
            Text("━━━ SYSTEM DIAGNOSTICS ━━━", style=f"bold {THEME['accent']}"),
            Text(""),
            table,
            Text(""),
            Text("━━━ DESCRIPTION ━━━", style=f"bold {THEME['accent']}"),
            Text(f"\n{desc_esc}\n", style=THEME['fg']),
            Text("━━━ QUICK CONTROLS ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • [bold {THEME['primary']}]↑ / ↓[/bold {THEME['primary']}]   : Move cursor\n  • [bold {THEME['primary']}]ENTER[/bold {THEME['primary']}]   : Select option\n  • [bold {THEME['primary']}]ESC[/bold {THEME['primary']}]     : Go back / Exit\n", style=THEME['fg'])
        ]

    elif context_type == "settings":
        title_text = "Configuration Help"
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column("Player", style=f"bold {THEME['fg']}")
        table.add_column("Status", style=THEME['success'])
        
        table.add_row("MPV Player", "[green]Installed[/green]" if mpv_ok else "[red]Not Found[/red]")
        table.add_row("VLC Player", "[green]Installed[/green]" if vlc_ok else "[red]Not Found[/red]")
        if sys.platform == "darwin":
            table.add_row("IINA Player", "[green]Installed[/green]" if iina_ok else "[red]Not Found[/red]")
        else:
            table.add_row("Celluloid", "[green]Installed[/green]" if celluloid_ok else "[red]Not Found[/red]")
            table.add_row("Haruna", "[green]Installed[/green]" if haruna_ok else "[red]Not Found[/red]")

        desc = ""
        option_title = options[selected_idx] if selected_idx < len(options) else ""
        if "Preferred Player" in option_title:
            desc = "Choose your preferred video player. MPV is strongly recommended as it supports embedding and progress tracking. VLC is used as a fallback."
        elif "Default Video Quality" in option_title:
            desc = "Select your default streaming quality. If 'auto' is selected, the highest resolution available on the server will be selected."
        elif "Cookie Sync Browser" in option_title:
            desc = "Select which browser to extract cookies from (Chrome or Edge). Cookies are required to bypass Cloudflare Turnstile protection on WitAnime/Anime3rb."
        elif "History Tracking" in option_title:
            desc = "Toggle local watch history tracking. If disabled, watched episodes and watch progress won't be saved in the database."
        elif "Auto Fullscreen" in option_title:
            desc = "Automatically open the player in fullscreen mode upon launch."
        elif "Custom Player Args" in option_title:
            desc = "Specify additional command line arguments to pass directly to the player executable (e.g. `--volume=80` or `--ontop`)."
        elif "Nerd Font Icons" in option_title:
            desc = "Toggle Nerd Font icons support. If your terminal font supports Nerd Fonts, this enables modern high-resolution icons instead of standard Unicode."
        elif "Accounts Integration" in option_title:
            desc = "Link and manage MyAnimeList (MAL) or AniList accounts to sync your watch progress dynamically."
        elif "Clear Search History" in option_title:
            desc = "Delete all recent search queries cached in your local config.json file."
        elif "Clear All Watch History" in option_title:
            desc = "Warning: This will permanently delete all local bookmarks, watched episode lists, watch progress, and linked accounts from the database."
        elif "Go Back" in option_title:
            desc = "Return to the main menu."

        desc_esc = escape(desc)
        renderables = [
            Text("━━━ PLAYER DIAGNOSTICS ━━━", style=f"bold {THEME['accent']}"),
            Text(""),
            table,
            Text(""),
            Text("━━━ SETTING HELP ━━━", style=f"bold {THEME['accent']}"),
            Text(f"\n{desc_esc}\n", style=THEME['fg'])
        ]
        
    elif context_type == "search_results":
        title_text = "Search Details"
        query = metadata.get("search_query", "")
        selected_opt = options[selected_idx] if selected_idx < len(options) else ""
        provider = "WitAnime" if "[WitAnime]" in selected_opt else ("Anime3rb" if "[Anime3rb]" in selected_opt else "Unknown")
        
        query_esc = escape(query)
        provider_esc = escape(provider)
        title_esc = escape(selected_opt.replace(f'[{provider}] ', ''))
        
        renderables = [
            Text("━━━ SEARCH CONTEXT ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • [bold {THEME['primary']}]Active Query[/bold {THEME['primary']}] : '{query_esc}'\n  • [bold {THEME['primary']}]Total Results[/bold {THEME['primary']}]: {len(options)} item(s) found\n", style=THEME['fg']),
            Text("━━━ SELECTED ITEM ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • [bold {THEME['primary']}]Provider[/bold {THEME['primary']}]     : {provider_esc}\n  • [bold {THEME['primary']}]Title[/bold {THEME['primary']}]        : {title_esc}\n", style=THEME['fg']),
            Text("━━━ INSTRUCTIONS ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • Press [bold {THEME['success']}]ENTER[/bold {THEME['success']}] to load this anime's episode list.\n  • Press [bold {THEME['warning']}]ESC[/bold {THEME['warning']}] to return to the search input screen.\n", style=THEME['fg'])
        ]

    elif context_type == "favorites":
        title_text = "Bookmark Details"
        selected_opt = options[selected_idx] if selected_idx < len(options) else ""
        show_slug = metadata.get("fav_slugs", [])[selected_idx] if selected_idx < len(metadata.get("fav_slugs", [])) else None
        
        title_esc = escape(selected_opt)
        info_text_markup = f"\n  • [bold {THEME['primary']}]Title[/bold {THEME['primary']}]        : {title_esc}\n"
        if show_slug:
            hist = get_watch_history(show_slug)
            last_ep = hist.get("last_watched", 0)
            watched_eps = len(hist.get("watched", []))
            info_text_markup += f"  • [bold {THEME['primary']}]Last Watched[/bold {THEME['primary']}]   : Episode {last_ep if last_ep > 0 else 'None'}\n"
            info_text_markup += f"  • [bold {THEME['primary']}]Watched Count[/bold {THEME['primary']}]  : {watched_eps} episode(s)\n"

        renderables = [
            Text("━━━ LIBRARY STATS ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • [bold {THEME['primary']}]Total Bookmarks[/bold {THEME['primary']}] : {len(options)} show(s)\n", style=THEME['fg']),
            Text("━━━ SELECTED BOOKMARK ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(info_text_markup, style=THEME['fg']),
            Text("━━━ INSTRUCTIONS ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • Press [bold {THEME['success']}]ENTER[/bold {THEME['success']}] to open this show's episodes list.\n  • Press [bold {THEME['warning']}]ESC[/bold {THEME['warning']}] to return to the main menu.\n", style=THEME['fg'])
        ]

    elif context_type == "episode_selection":
        title_text = "Episode Selection & Controls"
        anime_title = metadata.get("anime_title", "Anime Show")
        provider = metadata.get("provider", "Unknown")
        player_name = metadata.get("player_name", "MPV")
        
        anime_title_esc = escape(anime_title)
        provider_esc = escape(provider)
        player_name_esc = escape(player_name)
        
        guide_text_markup = ""
        if player_name == "MPV":
            guide_text_markup += f"\n  • [bold {THEME['primary']}]SPACE[/bold {THEME['primary']}]         : Play / Pause\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]LEFT / RIGHT[/bold {THEME['primary']}]  : Seek Back / Forward (5s)\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]UP / DOWN[/bold {THEME['primary']}]     : Seek Back / Forward (1m)\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]9 / 0[/bold {THEME['primary']}]         : Adjust Volume\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]F[/bold {THEME['primary']}]             : Toggle Fullscreen\n"
            guide_text_markup += f"  • [bold {THEME['error']}]Q[/bold {THEME['error']}]             : Close Player / Stop\n"
        else: # VLC
            guide_text_markup += f"\n  • [bold {THEME['primary']}]SPACE[/bold {THEME['primary']}]         : Play / Pause\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]ALT + ◄ / ◄[/bold {THEME['primary']}]  : Seek Back / Forward\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]CTRL + ▲ / ▼[/bold {THEME['primary']}] : Adjust Volume\n"
            guide_text_markup += f"  • [bold {THEME['primary']}]F[/bold {THEME['primary']}]             : Toggle Fullscreen\n"
            guide_text_markup += f"  • [bold {THEME['error']}]Q / ESC[/bold {THEME['error']}]       : Close Player / Stop\n"

        renderables = [
            Text("━━━ SHOW METADATA ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • [bold {THEME['primary']}]Title[/bold {THEME['primary']}]        : {anime_title_esc}\n  • [bold {THEME['primary']}]Provider[/bold {THEME['primary']}]     : {provider_esc}\n", style=THEME['fg']),
            Text(f"━━━ {player_name_esc.upper()} HOTKEYS GUIDE ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(guide_text_markup, style=THEME['fg']),
            Text("━━━ INSTRUCTIONS ━━━", style=f"bold {THEME['accent']}"),
            Text.from_markup(f"\n  • Press [bold {THEME['primary']}]SPACE[/bold {THEME['primary']}] on an episode to select/deselect it.\n  • Press [bold {THEME['primary']}]A[/bold {THEME['primary']}] to toggle selection of ALL episodes.\n  • Press [bold {THEME['primary']}]F[/bold {THEME['primary']}] to toggle bookmark/favorite status.\n  • Press [bold {THEME['success']}]ENTER[/bold {THEME['success']}] to start scraping and play selected.\n", style=THEME['fg'])
        ]

    return Panel(
        Group(*renderables),
        title=f"[bold {THEME['primary']}] {title_text} [/bold {THEME['primary']}]",
        border_style=THEME['border'],
        box=rich_box.ROUNDED,
        padding=(1, 3),
        expand=True
    )

def interactive_select(options, title="Select Option", context_type=None, metadata=None):
    if not options:
        return -1, None

    flush_input_buffer()
    selected_idx = 0
    scroll_offset = 0
    max_visible = 12

    if sys.stdout.isatty():
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

    try:
        def make_panel():
            nonlocal scroll_offset
            if selected_idx < scroll_offset:
                scroll_offset = selected_idx
            elif selected_idx >= scroll_offset + max_visible:
                scroll_offset = selected_idx - max_visible + 1

            table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))

            if scroll_offset > 0:
                table.add_row(f"[dim {THEME['dim']}]    {get_icon('arrow_up')}more items above[/dim {THEME['dim']}]")
            else:
                table.add_row("")

            visible_options = options[scroll_offset : scroll_offset + max_visible]
            for idx_rel, opt in enumerate(visible_options):
                idx_abs = scroll_offset + idx_rel
                num_label = f"{idx_abs + 1}."
                if idx_abs == selected_idx:
                    table.add_row(f"  [bold {THEME['accent']}]{get_icon('bullet')}[/bold {THEME['accent']}][bold {THEME['select_fg']} on {THEME['select_bg']}] {num_label} {opt} [/bold {THEME['select_fg']} on {THEME['select_bg']}]")
                else:
                    table.add_row(f"    [{THEME['dim']}]{num_label}[/{THEME['dim']}] [{THEME['fg']}]{opt}[/{THEME['fg']}]")

            if scroll_offset + max_visible < len(options):
                table.add_row(f"[dim {THEME['dim']}]    {get_icon('arrow_down')}more items below[/dim {THEME['dim']}]")
            else:
                table.add_row("")

            page_info = f"({selected_idx + 1}/{len(options)})"

            left_panel = Panel(
                table,
                title=f"[bold {THEME['primary']}] {title} [/bold {THEME['primary']}][{THEME['dim']}]{page_info}[/{THEME['dim']}]",
                subtitle=f"[dim {THEME['dim']}]↑↓ Navigate  ⏎ Select  Esc Back[/dim {THEME['dim']}]",
                border_style=THEME['border'],
                box=rich_box.ROUNDED,
                expand=True,
                padding=(1, 3)
            )

            width, height = shutil.get_terminal_size()
            if not context_type or width < 90 or height < 18:
                left_panel.expand = False
                return left_panel

            right_panel = get_context_panel(context_type, selected_idx, options, metadata)

            grid = Table.grid(expand=True)
            grid.add_column(ratio=45)
            grid.add_column(ratio=55)
            grid.add_row(left_panel, right_panel)
            return grid

        with RawModeContext():
            with Live(make_panel(), refresh_per_second=15, transient=True) as live:
                while True:
                    key = read_key()
                    if key == KEY_UP:
                        selected_idx = (selected_idx - 1) % len(options)
                        live.update(make_panel())
                    elif key == KEY_DOWN:
                        selected_idx = (selected_idx + 1) % len(options)
                        live.update(make_panel())
                    elif key == KEY_ENTER:
                        return selected_idx, options[selected_idx]
                    elif key in (KEY_ESC, KEY_CTRL_C):
                        return -1, None
    finally:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()


def interactive_checklist(options, title="Select Episodes", default_start_idx=0, is_favorite=False, on_toggle_favorite=None, context_type=None, metadata=None):
    if not options:
        return []

    flush_input_buffer()
    selected_idx = default_start_idx
    scroll_offset = 0
    max_visible = 12
    checked = [False] * len(options)
    if len(checked) > 0:
        checked[selected_idx] = True

    if sys.stdout.isatty():
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

    try:
        def make_panel():
            nonlocal scroll_offset
            if selected_idx < scroll_offset:
                scroll_offset = selected_idx
            elif selected_idx >= scroll_offset + max_visible:
                scroll_offset = selected_idx - max_visible + 1

            table = Table(box=None, show_header=False, pad_edge=False)

            if scroll_offset > 0:
                table.add_row(f"[dim {THEME['dim']}]  {get_icon('arrow_up')}more items above[/dim {THEME['dim']}]")
            else:
                table.add_row("")

            visible_options = options[scroll_offset : scroll_offset + max_visible]
            for idx_rel, opt in enumerate(visible_options):
                idx_abs = scroll_offset + idx_rel
                box = get_icon("check") if checked[idx_abs] else get_icon("cross")
                color = THEME["checked"] if checked[idx_abs] else THEME["unchecked"]
                opt_text = f"{box}{opt}"

                if idx_abs == selected_idx:
                    table.add_row(f"[bold {THEME['primary']}]{get_icon('bullet')}[/bold {THEME['primary']}] [bold {THEME['select_fg']} on {THEME['select_bg']}]{opt_text}[/bold {THEME['select_fg']} on {THEME['select_bg']}]")
                else:
                    table.add_row(f"  [{color}]{opt_text}[/{color}]")

            if scroll_offset + max_visible < len(options):
                table.add_row(f"[dim {THEME['dim']}]  {get_icon('arrow_down')}more items below[/dim {THEME['dim']}]")
            else:
                table.add_row("")

            table.add_row("")

            sel_count = sum(checked)
            page_info = f"({selected_idx + 1}/{len(options)}) [{sel_count} selected]"

            fav_icon = f" [bold {THEME['error']}]{get_icon('favorite_on')}[/bold {THEME['error']}]" if is_favorite else f" [{THEME['dim']}]{get_icon('favorite_off')}[/{THEME['dim']}]"

            left_panel = Panel(
                table,
                title=f"[bold {THEME['primary']}] {title}{fav_icon} {page_info}[/bold {THEME['primary']}]",
                subtitle=f"[dim {THEME['dim']}]SPACE=toggle  SPACE+↕=drag  A=all  F=fav  ENTER=confirm  ESC=back[/dim {THEME['dim']}]",
                border_style=THEME['border'],
                box=rich_box.ROUNDED,
                expand=True,
                padding=(1, 2)
            )

            width, height = shutil.get_terminal_size()
            if not context_type or width < 90 or height < 18:
                left_panel.expand = False
                return left_panel

            right_panel = get_context_panel(context_type, selected_idx, options, metadata)

            grid = Table.grid(expand=True)
            grid.add_column(ratio=45)
            grid.add_column(ratio=55)
            grid.add_row(left_panel, right_panel)
            return grid

        last_space_time = 0.0
        last_space_toggled_idx = None

        def is_space_physically_held():
            if os.name == 'nt':
                try:
                    import ctypes
                    return bool(ctypes.windll.user32.GetAsyncKeyState(0x20) & 0x8000)
                except Exception:
                    pass
            return False

        with RawModeContext():
            with Live(make_panel(), refresh_per_second=15, transient=True) as live:
                while True:
                    is_held = is_space_physically_held()
                    if time.time() - last_space_time < 0.25:
                        is_held = True

                    key = read_key()
                    is_held = is_held or is_space_physically_held()
                    if time.time() - last_space_time < 0.25:
                        is_held = True

                    if key == KEY_UP:
                        selected_idx = (selected_idx - 1) % len(options)
                        if is_held:
                            checked[selected_idx] = not checked[selected_idx]
                            last_space_toggled_idx = selected_idx
                        live.update(make_panel())
                    elif key == KEY_DOWN:
                        selected_idx = (selected_idx + 1) % len(options)
                        if is_held:
                            checked[selected_idx] = not checked[selected_idx]
                            last_space_toggled_idx = selected_idx
                        live.update(make_panel())
                    elif key == KEY_SPACE:
                        current_time = time.time()
                        if (current_time - last_space_time > 0.4) or (selected_idx != last_space_toggled_idx):
                            checked[selected_idx] = not checked[selected_idx]
                            last_space_toggled_idx = selected_idx
                        last_space_time = current_time
                        live.update(make_panel())
                    elif key == KEY_A:
                        last_space_toggled_idx = None
                        all_checked = all(checked)
                        checked = [not all_checked] * len(options)
                        live.update(make_panel())
                    elif key in ('f', 'F'):
                        last_space_toggled_idx = None
                        if on_toggle_favorite:
                            is_favorite = on_toggle_favorite()
                            live.update(make_panel())
                    elif key == KEY_ENTER:
                        return [idx for idx, val in enumerate(checked) if val]
                    elif key in (KEY_ESC, KEY_CTRL_C):
                        return []
                    else:
                        last_space_toggled_idx = None
    finally:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()



# ════════════════════════════════════════════════════════════
#  Browser Cookie Extraction (Windows Chrome/Edge)
# ════════════════════════════════════════════════════════════

def get_user_data_path(browser_name):
    home = os.path.expanduser("~")
    if os.name == 'nt':
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            return None
        if browser_name == "chrome":
            return os.path.join(local_app_data, r"Google\Chrome\User Data")
        elif browser_name == "edge":
            return os.path.join(local_app_data, r"Microsoft\Edge\User Data")
    elif sys.platform == "darwin": # macOS
        if browser_name == "chrome":
            return os.path.join(home, "Library/Application Support/Google/Chrome")
        elif browser_name == "edge":
            return os.path.join(home, "Library/Application Support/Microsoft Edge")
    else: # Linux
        if browser_name == "chrome":
            return os.path.join(home, ".config/google-chrome")
        elif browser_name == "edge":
            return os.path.join(home, ".config/microsoft-edge")
    return None

def get_macos_key(browser_name):
    service = "Chrome Safe Storage" if browser_name == "chrome" else "Microsoft Edge Safe Storage"
    try:
        cmd = ["security", "find-generic-password", "-w", "-s", service]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip().encode("utf-8")
    except Exception:
        try:
            import keyring
            key = keyring.get_password(service, "Chrome" if browser_name == "chrome" else "Microsoft Edge")
            if key:
                return key.encode("utf-8")
        except Exception:
            pass
    return None

def get_linux_key(browser_name):
    service = "Chrome Safe Storage" if browser_name == "chrome" else "Microsoft Edge Safe Storage"
    account = "Chrome" if browser_name == "chrome" else "edge"
    try:
        cmd = ["secret-tool", "lookup", "service", service, "account", account]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            return result.stdout.strip().encode("utf-8")
    except Exception:
        pass

    try:
        cmd = ["secret-tool", "lookup", "xdg:schema", "org.chromium.Chromium.SafeStorage", "password", "chrome" if browser_name == "chrome" else "edge"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            return result.stdout.strip().encode("utf-8")
    except Exception:
        pass

    try:
        import keyring
        key = keyring.get_password(service, account)
        if key:
            return key.encode("utf-8")
    except Exception:
        pass

    return b"peanuts"

def decrypt_cbc_cookie(encrypted_value, key):
    if not encrypted_value:
        return ""
    if not AES:
        return ""
    if encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11"):
        ciphertext = encrypted_value[3:]
    else:
        ciphertext = encrypted_value

    try:
        cipher = AES.new(key, AES.MODE_CBC, iv=b' ' * 16)
        decrypted = cipher.decrypt(ciphertext)
        padding_len = decrypted[-1]
        if 1 <= padding_len <= 16:
            if all(x == padding_len for x in decrypted[-padding_len:]):
                decrypted = decrypted[:-padding_len]
        return decrypted.decode("utf-8", errors="ignore")
    except Exception:
        return ""

def get_browser_cookies(browser_name):
    user_data_path = get_user_data_path(browser_name)
    if not user_data_path or not os.path.exists(user_data_path):
        return []

    decrypted_key = None
    is_gcm = False

    if os.name == 'nt':
        local_state_path = os.path.join(user_data_path, "Local State")
        if os.path.exists(local_state_path):
            try:
                with open(local_state_path, "r", encoding="utf-8") as f:
                    local_state = json.loads(f.read())
                encrypted_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
                encrypted_key = encrypted_key[5:]
                if win32crypt:
                    decrypted_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
                    is_gcm = True
            except Exception:
                pass
    elif sys.platform == "darwin":
        password = get_macos_key(browser_name)
        if password:
            import hashlib
            decrypted_key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
            is_gcm = False
    else: # Linux
        password = get_linux_key(browser_name)
        if password:
            import hashlib
            decrypted_key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
            is_gcm = False

    if not decrypted_key:
        return []

    cookies = {}
    profiles = ["Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"]

    try:
        for item in os.listdir(user_data_path):
            if (item.startswith("Profile") or item == "Default") and os.path.isdir(os.path.join(user_data_path, item)):
                if item not in profiles:
                    profiles.append(item)
    except Exception:
        pass

    for profile in profiles:
        cookie_path = os.path.join(user_data_path, profile, "Network", "Cookies")
        if not os.path.exists(cookie_path):
            cookie_path = os.path.join(user_data_path, profile, "Cookies")
        if not os.path.exists(cookie_path):
            continue

        temp_cookie_file = None
        try:
            temp_cookie_file = tempfile.mktemp(suffix=".db")
            shutil.copy2(cookie_path, temp_cookie_file)
        except Exception:
            continue

        try:
            conn = sqlite3.connect(temp_cookie_file)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name, encrypted_value, host_key FROM cookies WHERE host_key LIKE '%anime3rb.com%' OR host_key LIKE '%vid3rb.com%' OR host_key LIKE '%witanime%'"
            )
            for name, encrypted_value, host_key in cursor.fetchall():
                try:
                    if is_gcm:
                        if encrypted_value[:3] == b'v10' or encrypted_value[:3] == b'v11':
                            nonce = encrypted_value[3:15]
                            ciphertext = encrypted_value[15:-16]
                            tag = encrypted_value[-16:]
                            cipher = AES.new(decrypted_key, AES.MODE_GCM, nonce=nonce)
                            value = cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
                        else:
                            if win32crypt:
                                value = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1].decode("utf-8")
                            else:
                                continue
                    else:
                        value = decrypt_cbc_cookie(encrypted_value, decrypted_key)

                    domain = host_key
                    if not domain.startswith("."):
                        domain = "." + domain

                    cookies[f"{domain}:{name}"] = {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/"
                    }
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
        finally:
            if temp_cookie_file:
                try:
                    os.remove(temp_cookie_file)
                except Exception:
                    pass

    return list(cookies.values())


def get_preferred_cookies():
    cfg = load_config()
    pref = cfg.get("preferred_browser", "auto")
    if pref == "chrome":
        return get_browser_cookies("chrome")
    elif pref == "edge":
        return get_browser_cookies("edge")
    else: # auto
        return get_browser_cookies("chrome") or get_browser_cookies("edge")


# ════════════════════════════════════════════════════════════
#  PATH Refresh (needed after installing software)
# ════════════════════════════════════════════════════════════

def refresh_system_path():
    """Refresh os.environ['PATH'] from the Windows registry.
    After winget/choco/scoop installs a program, the PATH is updated
    in the registry but NOT in the current running process.
    This function re-reads it so shutil.which() can find newly installed programs."""
    if os.name != 'nt':
        return
    try:
        import winreg
        parts = []
        # System PATH
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
                sys_path, _ = winreg.QueryValueEx(key, "Path")
                parts.append(sys_path)
        except Exception:
            pass
        # User PATH
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                usr_path, _ = winreg.QueryValueEx(key, "Path")
                parts.append(usr_path)
        except Exception:
            pass
        if parts:
            os.environ['PATH'] = ";".join(parts)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
#  Player Discovery
# ════════════════════════════════════════════════════════════

def find_vlc():
    vlc_path = shutil.which("vlc")
    if vlc_path:
        return vlc_path

    if os.name == 'nt':
        # Check Windows Registry
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vlc.exe")
            vlc_path, _ = winreg.QueryValueEx(key, "")
            if vlc_path and os.path.exists(vlc_path):
                return vlc_path
        except Exception:
            pass

        # Scan common installation paths
        common_paths = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\VideoLAN\VLC\vlc.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\VideoLAN\VLC\vlc.exe"),
            # Chocolatey
            r"C:\ProgramData\chocolatey\bin\vlc.exe",
            # Scoop
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\vlc\current\vlc.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\shims\vlc.exe"),
        ]
        for p in common_paths:
            if os.path.exists(p):
                return p
    return None


def find_mpv():
    """Find mpv or mpvnet executable. Returns the path or None."""
    # 1. Check PATH for mpv and mpvnet
    for exe_name in ("mpv", "mpvnet"):
        found = shutil.which(exe_name)
        if found:
            return found

    if os.name == 'nt':
        # 2. Check Windows Registry for both mpv.exe and mpvnet.exe
        for reg_exe in ("mpv.exe", "mpvnet.exe"):
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{reg_exe}")
                val, _ = winreg.QueryValueEx(key, "")
                if val and os.path.exists(val):
                    return val
            except Exception:
                pass

        # 3. Scan ALL common installation directories
        home = os.path.expanduser("~")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        programfiles = os.environ.get("ProgramFiles", r"C:\Program Files")
        programfiles86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

        common_paths = [
            # Standard mpv paths
            os.path.join(programfiles, "mpv", "mpv.exe"),
            os.path.join(programfiles86, "mpv", "mpv.exe"),
            os.path.join(localappdata, "Programs", "mpv", "mpv.exe"),
            os.path.join(localappdata, "mpv", "mpv.exe"),
            # mpv.net paths (installed by winget as "mpv.net")
            os.path.join(programfiles, "mpv.net", "mpvnet.exe"),
            os.path.join(programfiles86, "mpv.net", "mpvnet.exe"),
            os.path.join(localappdata, "Programs", "mpv.net", "mpvnet.exe"),
            os.path.join(localappdata, "mpv.net", "mpvnet.exe"),
            os.path.join(appdata, "mpv.net", "mpvnet.exe"),
            # Chocolatey
            r"C:\ProgramData\chocolatey\bin\mpv.exe",
            r"C:\ProgramData\chocolatey\lib\mpv\tools\mpv.exe",
            # Scoop
            os.path.join(home, "scoop", "apps", "mpv", "current", "mpv.exe"),
            os.path.join(home, "scoop", "shims", "mpv.exe"),
            # winget typical install locations
            os.path.join(localappdata, "Microsoft", "WinGet", "Packages"),
        ]

        for p in common_paths:
            if os.path.isfile(p):
                return p

        # 4. Deep-scan winget packages directory for mpv/mpvnet executables
        winget_pkgs = os.path.join(localappdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkgs):
            try:
                for root, dirs, files in os.walk(winget_pkgs):
                    for fname in files:
                        if fname.lower() in ("mpv.exe", "mpvnet.exe"):
                            return os.path.join(root, fname)
            except Exception:
                pass

        # 5. Deep-scan Program Files for mpv
        for pf in (programfiles, programfiles86, localappdata):
            if not pf or not os.path.isdir(pf):
                continue
            try:
                for item in os.listdir(pf):
                    if "mpv" in item.lower():
                        candidate_dir = os.path.join(pf, item)
                        if os.path.isdir(candidate_dir):
                            for exe in ("mpv.exe", "mpvnet.exe"):
                                full = os.path.join(candidate_dir, exe)
                                if os.path.isfile(full):
                                    return full
            except Exception:
                pass

    else:
        # Linux — also check common binary locations
        for p in ("/usr/bin/mpv", "/usr/local/bin/mpv", "/snap/bin/mpv"):
            if os.path.isfile(p):
                return p

    return None

def find_iina():
    iina_path = shutil.which("iina")
    if iina_path:
        return iina_path
    if sys.platform == "darwin":
        common_paths = [
            "/Applications/IINA.app/Contents/MacOS/IINA",
            os.path.expanduser("~/Applications/IINA.app/Contents/MacOS/IINA")
        ]
        for p in common_paths:
            if os.path.exists(p):
                return p
    return None

def find_celluloid():
    return shutil.which("celluloid")

def find_haruna():
    return shutil.which("haruna")


# ════════════════════════════════════════════════════════════
#  Player Installation (Auto-Download)
# ════════════════════════════════════════════════════════════

def install_player(player_name):
    """Attempt to automatically install VLC, MPV, IINA, Celluloid, or Haruna.
    After successful installation, refreshes PATH so they can be located."""
    console.print(f"\n[bold {THEME['primary']}]{get_icon('watch_history')}Attempting to install {player_name.upper()}...[/bold {THEME['primary']}]")

    installed = False

    # ── Windows Checks and Warnings for Linux-Only/macOS-Only Players ──
    if os.name == 'nt' and player_name in ["iina", "celluloid", "haruna"]:
        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}{player_name.upper()} is not natively supported on Windows.[/bold {THEME['error']}]")
        if player_name == "iina":
            console.print(f"[bold {THEME['warning']}]IINA is a macOS-only player.[/bold {THEME['warning']}]")
        else:
            console.print(f"[bold {THEME['warning']}]{player_name.upper()} is a Linux-oriented player. We recommend installing MPV or VLC on Windows.[/bold {THEME['warning']}]")
        time.sleep(4.0)
        return False

    # ── macOS installation via Homebrew ──
    if sys.platform == "darwin":
        if shutil.which("brew"):
            try:
                # Install casks for GUI players, normal formulas otherwise
                is_cask = player_name in ["iina", "vlc", "mpv", "celluloid", "haruna"]
                cmd = ["brew", "install"]
                if is_cask:
                    cmd.append("--cask")
                cmd.append(player_name)
                console.print(f"[{THEME['dim']}]  Trying brew install {player_name}...[/{THEME['dim']}]")
                result = subprocess.run(cmd, timeout=300)
                if result.returncode == 0:
                    console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via Homebrew![/bold {THEME['success']}]")
                    time.sleep(2.0)
                    return True
            except Exception:
                pass
        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Could not auto-install {player_name.upper()} on macOS.[/bold {THEME['error']}]")
        console.print(f"[bold {THEME['warning']}]{get_icon('warning')}Please install it manually from the official website or using Homebrew.[/bold {THEME['warning']}]")
        time.sleep(3.0)
        return False

    # ── Windows installation ──
    if os.name == 'nt':
        # ── Try Chocolatey first ──
        try:
            choco_pkg = "mpv" if player_name == "mpv" else "vlc"
            cmd = ["choco", "install", choco_pkg, "-y"]
            console.print(f"[{THEME['dim']}]  Trying choco install {choco_pkg}...[/{THEME['dim']}]")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via Chocolatey![/bold {THEME['success']}]")
                installed = True
        except FileNotFoundError:
            console.print(f"[{THEME['dim']}]  choco not found, trying alternatives...[/{THEME['dim']}]")
        except subprocess.TimeoutExpired:
            console.print(f"[{THEME['dim']}]  choco timed out[/{THEME['dim']}]")

        # ── Try Scoop ──
        if not installed:
            try:
                scoop_pkg = "mpv" if player_name == "mpv" else "vlc"
                cmd = ["scoop", "install", scoop_pkg]
                console.print(f"[{THEME['dim']}]  Trying scoop install {scoop_pkg}...[/{THEME['dim']}]")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via Scoop![/bold {THEME['success']}]")
                    installed = True
            except FileNotFoundError:
                console.print(f"[{THEME['dim']}]  scoop not found, trying alternatives...[/{THEME['dim']}]")
            except subprocess.TimeoutExpired:
                console.print(f"[{THEME['dim']}]  scoop timed out[/{THEME['dim']}]")

        # ── Try winget ──
        if not installed:
            try:
                if player_name == "mpv":
                    winget_id = "mpv.net"
                else:
                    winget_id = "VideoLAN.VLC"

                cmd = [
                    "winget", "install", "--id", winget_id, "-e",
                    "--accept-source-agreements", "--accept-package-agreements",
                    "--silent"
                ]
                console.print(f"[{THEME['dim']}]  Trying winget install {winget_id}...[/{THEME['dim']}]")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via winget![/bold {THEME['success']}]")
                    installed = True
                else:
                    console.print(f"[{THEME['dim']}]  winget returned code {result.returncode}[/{THEME['dim']}]")
            except FileNotFoundError:
                console.print(f"[{THEME['dim']}]  winget not found[/{THEME['dim']}]")
            except subprocess.TimeoutExpired:
                console.print(f"[{THEME['dim']}]  winget timed out[/{THEME['dim']}]")

        # ── Refresh PATH from registry so find_mpv()/find_vlc() works ──
        if installed:
            console.print(f"[{THEME['dim']}]  Refreshing system PATH...[/{THEME['dim']}]")
            refresh_system_path()
            time.sleep(2.0)
            # Verify we can actually find it now
            found = find_mpv() if player_name == "mpv" else find_vlc()
            if found:
                console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} verified at: {found}[/bold {THEME['success']}]")
            else:
                console.print(f"[bold {THEME['warning']}]{get_icon('warning')}Installed but path not detected yet. Searching deeper...[/bold {THEME['warning']}]")
                time.sleep(1.0)
            return True

        # All methods failed
        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Could not auto-install {player_name.upper()} on Windows.[/bold {THEME['error']}]")
        console.print(f"[bold {THEME['warning']}]{get_icon('warning')}Please install it manually:[/bold {THEME['warning']}]")
        if player_name == "mpv":
            console.print(f"[{THEME['dim']}]  Download: https://mpv.io/installation/[/{THEME['dim']}]")
            console.print(f"[{THEME['dim']}]  Or run: winget install mpv.net[/{THEME['dim']}]")
        else:
            console.print(f"[{THEME['dim']}]  Download: https://www.videolan.org/vlc/[/{THEME['dim']}]")
            console.print(f"[{THEME['dim']}]  Or run: winget install VideoLAN.VLC[/{THEME['dim']}]")
        time.sleep(3.0)
        return False

    # ── Linux installation ──
    else:
        # Linux — detect package manager
        pkg_managers = [
            ("apt", ["sudo", "apt", "install", "-y", player_name]),
            ("dnf", ["sudo", "dnf", "install", "-y", player_name]),
            ("pacman", ["sudo", "pacman", "-S", "--noconfirm", player_name]),
            ("zypper", ["sudo", "zypper", "install", "-y", player_name]),
            ("apk", ["sudo", "apk", "add", player_name]),
            ("emerge", ["sudo", "emerge", player_name]),
            ("xbps-install", ["sudo", "xbps-install", "-y", player_name]),
        ]

        for pm_name, cmd in pkg_managers:
            if shutil.which(pm_name):
                try:
                    console.print(f"[{THEME['dim']}]  Using {pm_name} to install {player_name}...[/{THEME['dim']}]")
                    result = subprocess.run(cmd, timeout=300)
                    if result.returncode == 0:
                        console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via {pm_name}![/bold {THEME['success']}]")
                        time.sleep(2.0)
                        return True
                except FileNotFoundError:
                    continue
                except subprocess.TimeoutExpired:
                    continue

        # Flatpak fallback
        if shutil.which("flatpak"):
            try:
                if player_name == "vlc":
                    flatpak_id = "org.videolan.VLC"
                elif player_name == "mpv":
                    flatpak_id = "io.mpv.Mpv"
                elif player_name == "celluloid":
                    flatpak_id = "io.github.celluloid_player.Celluloid"
                elif player_name == "haruna":
                    flatpak_id = "org.kde.haruna"
                else:
                    flatpak_id = None

                if flatpak_id:
                    cmd = ["flatpak", "install", "-y", flatpak_id]
                    console.print(f"[{THEME['dim']}]  Trying flatpak install {flatpak_id}...[/{THEME['dim']}]")
                    result = subprocess.run(cmd, timeout=300)
                    if result.returncode == 0:
                        console.print(f"[bold {THEME['success']}]{get_icon('check')}{player_name.upper()} installed successfully via Flatpak![/bold {THEME['success']}]")
                        time.sleep(2.0)
                        return True
            except Exception:
                pass

        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Could not auto-install {player_name.upper()} on this system.[/bold {THEME['error']}]")
        console.print(f"[bold {THEME['warning']}]{get_icon('warning')}Please install manually using your package manager.[/bold {THEME['warning']}]")
        time.sleep(3.0)
        return False


# ════════════════════════════════════════════════════════════
#  Player Launch (Detached)
# ════════════════════════════════════════════════════════════

def play_with_vlc(stream_urls):
    vlc_path = find_vlc()
    if not vlc_path:
        return False

    cfg = load_config()
    custom_args = cfg.get("custom_player_args", "").strip()
    user_args = custom_args.split() if custom_args else []

    fs_arg = ["--fullscreen"] if cfg.get("fullscreen", True) else []
    cmd = [vlc_path] + fs_arg + user_args + stream_urls
    try:
        if os.name == 'nt':
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def play_with_mpv(stream_urls, slug=None, ep=None):
    mpv_path = find_mpv()
    if not mpv_path:
        return False

    # mpvnet.exe uses slightly different args than mpv.exe
    is_mpvnet = "mpvnet" in os.path.basename(mpv_path).lower()

    cfg = load_config()
    custom_args = cfg.get("custom_player_args", "").strip()
    user_args = custom_args.split() if custom_args else []

    fs_arg = ["--fullscreen"] if cfg.get("fullscreen", True) else []

    ipc_path = None
    if slug and ep is not None:
        if os.name == 'nt':
            ipc_path = rf"\\.\pipe\pyanime-ipc-{slug}-{ep}"
        else:
            ipc_path = f"/tmp/pyanime-ipc-{slug}-{ep}.sock"
        fs_arg.append(f"--input-ipc-server={ipc_path}")

        # Check saved progress
        prog = get_episode_progress(slug, ep)
        if prog and prog.get("time_pos", 0) > 5:
            # resume playback
            fs_arg.append(f"--start={int(prog['time_pos'])}")

    if is_mpvnet:
        cmd = [mpv_path] + fs_arg + user_args + stream_urls
    else:
        cmd = [mpv_path, "--force-window", "--keep-open=yes"] + fs_arg + user_args + stream_urls

    try:
        if os.name == 'nt':
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
        else:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if ipc_path:
            import threading
            threading.Thread(target=poll_mpv_progress, args=(ipc_path, slug, ep), daemon=True).start()

        return True
    except Exception:
        return False

def play_with_iina(stream_urls):
    iina_path = find_iina()
    if not iina_path:
        return False
    cfg = load_config()
    custom_args = cfg.get("custom_player_args", "").strip()
    user_args = custom_args.split() if custom_args else []
    fs_arg = ["--mpv-fs"] if cfg.get("fullscreen", True) else []
    cmd = [iina_path] + fs_arg + user_args + stream_urls
    try:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def play_with_celluloid(stream_urls):
    celluloid_path = find_celluloid()
    if not celluloid_path:
        return False
    cfg = load_config()
    custom_args = cfg.get("custom_player_args", "").strip()
    user_args = custom_args.split() if custom_args else []
    fs_arg = ["--fullscreen"] if cfg.get("fullscreen", True) else []
    cmd = [celluloid_path] + fs_arg + user_args + stream_urls
    try:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def play_with_haruna(stream_urls):
    haruna_path = find_haruna()
    if not haruna_path:
        return False
    cfg = load_config()
    custom_args = cfg.get("custom_player_args", "").strip()
    user_args = custom_args.split() if custom_args else []
    fs_arg = ["--fullscreen"] if cfg.get("fullscreen", True) else []
    cmd = [haruna_path] + fs_arg + user_args + stream_urls
    try:
        subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ════════════════════════════════════════════════════════════
#  URL Utilities
# ════════════════════════════════════════════════════════════

def extract_slug(url):
    p = urlparse(url)
    if "anime3rb" in p.netloc:
        m = re.search(r"/titles/([^/#?]+)", p.path)
        if m: return m.group(1)
    elif "witanime" in p.netloc:
        m = re.search(r"/anime/([^/#?]+)", p.path)
        if m: return m.group(1)

    # Generic fallback: last non-empty path segment
    path = p.path.strip("/")
    if path:
        parts = path.split("/")
        if parts:
            return parts[-1]
    return None


def normalize(href, base_url=None):
    if href.startswith("http"):
        return href
    if base_url:
        p = urlparse(base_url)
        scheme_netloc = f"{p.scheme}://{p.netloc}"
        if href.startswith("/"):
            return scheme_netloc + href
        else:
            return scheme_netloc + "/" + href
    return href


def select_best_stream(urls):
    if not urls:
        return None

    cfg = load_config()
    pref_quality = cfg.get("default_quality", "auto")

    if pref_quality != "auto":
        if pref_quality == "1080p":
            keywords = ["1080p", "1080", "fhd", "w1080p"]
        elif pref_quality == "720p":
            keywords = ["720p", "720", "hd"]
        elif pref_quality == "480p":
            keywords = ["480p", "480", "sd"]
        else:
            keywords = []

        for u in urls:
            if any(kw in u.lower() for kw in keywords):
                return u

    # Default fallback: prefer highest quality
    for u in urls:
        if "1080" in u.lower() or "fhd" in u.lower():
            return u
    for u in urls:
        if "master.txt" in u or "/master." in u:
            return u
    for u in urls:
        if ".m3u8" in u or any(p in u for p in ["/hls/", "/hls2/", "/hls3/", "/index.m3u8", "/playlist."]):
            return u
    for u in urls:
        if ".mp4" in u:
            return u
    return urls[0]


# ════════════════════════════════════════════════════════════
#  Search Functions
# ════════════════════════════════════════════════════════════

async def search_anime3rb_async(query):
    query_enc = quote_plus(query)
    url = f"https://anime3rb.com/titles/list?q={query_enc}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            })
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/titles/" in href and "/titles/list" not in href and href not in seen:
                title_el = a.find("h4") or a.find("h2", class_="title-name") or a.find("h2")
                if title_el:
                    title = title_el.text.strip()
                else:
                    title = a.text.strip().replace("\n", " ")
                title = re.sub(r'\s+', ' ', title)
                if len(title) > 2:
                    seen.add(href)
                    results.append((title, href))
        return results
    except Exception:
        return []


async def search_witanime_async(query):
    query_enc = quote_plus(query)
    url = f"https://witanime.life/?search_param=animes&s={query_enc}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            })
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            title = a.text.strip().replace("\n", " ")
            title = re.sub(r'\s+', ' ', title)
            if "/anime/" in href and href not in seen and len(title) > 2:
                seen.add(href)
                results.append((title, href))
        return results
    except Exception:
        return []




# ════════════════════════════════════════════════════════════
#  Playwright Async Scraping Engine
# ════════════════════════════════════════════════════════════

async def fetch_episodes_list_async(url, is_witanime, active_cookies=None):
    from playwright.async_api import async_playwright

    slug = extract_slug(url)
    if not slug:
        return [], "Cannot extract slug from URL."

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                msg = str(e)
                if "executable doesn't exist" in msg.lower() or "playwright install" in msg.lower():
                    msg = "Playwright Chromium browser is not installed. Please run 'playwright install' or 'python3 -m playwright install' in your terminal."
                return [], msg

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            if active_cookies:
                try:
                    await context.add_cookies(active_cookies)
                except Exception:
                    pass

            page = await context.new_page()
            await page.set_viewport_size({"width": 1280, "height": 800})

            try:
                await page.goto(url, wait_until="load")

                # Cloudflare bypass loop
                success = False
                for _ in range(25):
                    title = await page.title()
                    if "Just a moment" not in title and "Attention Required" not in title:
                        if is_witanime == 1:
                            if await page.locator("div.episodes-card").count() > 0:
                                success = True
                                break
                        elif is_witanime == 0:
                            if await page.locator("a[href*='/episode/']").count() > 0:
                                success = True
                                break
                        else:
                            success = True
                            break
                    await asyncio.sleep(1.0)

                if not success:
                    await browser.close()
                    return [], "Failed to bypass Cloudflare challenge."

                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")
                eps = []

                if is_witanime == 1:
                    cards = soup.find_all("div", class_="episodes-card")
                    for idx, card in enumerate(cards):
                        title_anchor = card.find("h3").find("a") if card.find("h3") else None
                        if not title_anchor or not title_anchor.get("onclick"):
                            continue
                        onclick = title_anchor["onclick"]
                        try:
                            b64_str = onclick.split("'")[1]
                            ep_url = base64.b64decode(b64_str).decode("utf-8", errors="ignore")
                        except Exception:
                            continue
                        text = title_anchor.text.strip()
                        m = re.search(r'\d+', text)
                        ep_num = int(m.group(0)) if m else (idx + 1)

                        eps.append({
                            "episode": ep_num,
                            "page_url": ep_url
                        })
                elif is_witanime == 0:
                    seen = set()
                    for a in soup.find_all("a", href=True):
                        h = a["href"].strip()
                        m = re.search(rf"/episode/{re.escape(slug)}/(\d+)", h)
                        n = int(m.group(1)) if m else None
                        if n is not None and h not in seen:
                            seen.add(h)
                            eps.append({
                                "episode": n,
                                "page_url": normalize(h, base_url=url)
                            })

                eps.sort(key=lambda x: x["episode"])
                await browser.close()
                return eps, None
            except Exception as e:
                await browser.close()
                return [], str(e)
    except Exception as e:
        return [], str(e)



async def scrape_one_stream_async(browser, ep_item, is_witanime, active_cookies, results_dict, status_dict):
    ep_num = ep_item["episode"]
    url = ep_item["page_url"]

    status_dict[ep_num] = {"status": "Initializing...", "color": "cyan", "quality": "-"}

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    if active_cookies:
        try:
            await context.add_cookies(active_cookies)
        except Exception:
            pass

    page = await context.new_page()
    await page.set_viewport_size({"width": 1280, "height": 800})

    # Track media requests (MP4 / M3U8)
    media_requests = []
    def request_handler(request):
        u = request.url.lower()
        if any(kw in u for kw in ["google", "ads", "analytics", "banner", "p.gif", "count.gif", "tracker"]):
            return

        is_stream = False
        if request.resource_type == "media":
            is_stream = True
        elif ".mp4" in u or ".m3u8" in u or "master.txt" in u:
            is_stream = True
        elif request.resource_type in ["xhr", "fetch"]:
            if any(p in u for p in ["/hls/", "/hls2/", "/hls3/", "/master.", "/playlist.", "/index.m3u8"]):
                is_stream = True

        if is_stream and not u.startswith("blob:") and not u.startswith("data:"):
            media_requests.append(request.url)

    page.on("request", request_handler)

    resolved_stream = None
    try:
        status_dict[ep_num] = {"status": "Loading page...", "color": "blue", "quality": "-"}
        await page.goto(url, wait_until="load")

        if is_witanime == 1:
            status_dict[ep_num] = {"status": "Selecting server...", "color": "yellow", "quality": "-"}
            # WitAnime has watching servers
            await page.wait_for_selector("a.server-link", timeout=12000)
            server_links = page.locator("a.server-link")
            srv_count = await server_links.count()

            srv_items = []
            for idx in range(srv_count):
                loc = server_links.nth(idx)
                name = await loc.locator("span.ser").inner_text()
                srv_items.append({"index": idx, "name": name, "locator": loc})

            # Whitelist prioritised servers
            def get_priority(srv):
                name = srv["name"].lower()
                if "videa - fhd" in name or "videa-fhd" in name: return 5
                if "streamwish - fhd" in name or "streamwish-fhd" in name: return 4
                if "videa" in name: return 3
                if "streamwish" in name: return 2
                if "multi" in name: return 1
                return 0

            srv_items.sort(key=get_priority, reverse=True)

            for srv in srv_items:
                # Click server link via JS to bypass pointer overlays
                await page.evaluate("el => el.click()", await srv["locator"].element_handle())
                await asyncio.sleep(2.5)  # Wait for network

                # Check media requests
                if media_requests:
                    resolved_stream = select_best_stream(media_requests)
                    break

                # If no media request was captured directly on click, check the iframe source
                try:
                    iframe_src = await page.locator("#iframe-container iframe").get_attribute("src")
                except Exception:
                    iframe_src = None

                if iframe_src and iframe_src.startswith("http"):
                    status_dict[ep_num] = {"status": "Resolving player...", "color": "yellow", "quality": "-"}
                    # Open the player URL directly in a new top-level page to extract the stream
                    player_page = await context.new_page()
                    p_media = []

                    # Capture media requests
                    player_page.on("request", lambda r: p_media.append(r.url) if (
                        r.resource_type == "media" or
                        ".mp4" in r.url.lower() or
                        ".m3u8" in r.url.lower() or
                        "master.txt" in r.url.lower() or
                        (r.resource_type in ["xhr", "fetch"] and any(p in r.url.lower() for p in ["/hls/", "/hls2/", "/hls3/", "/master.", "/playlist.", "/index.m3u8"]))
                    ) and not r.url.lower().startswith("blob:") and not r.url.lower().startswith("data:") else None)

                    try:
                        await player_page.goto(iframe_src, wait_until="load")
                        await asyncio.sleep(2.0)

                        # Trigger interaction to force autoplay if needed
                        await player_page.mouse.click(640, 400)
                        try:
                            await player_page.evaluate("() => { const v = document.querySelector('video'); if(v) v.play(); }")
                        except Exception:
                            pass
                        await asyncio.sleep(3.0)

                        if p_media:
                            resolved_stream = select_best_stream(p_media)
                        else:
                            # DOM fallback
                            v_src = await player_page.evaluate("() => document.querySelector('video') ? document.querySelector('video').src : null")
                            if v_src and not v_src.startswith("blob:") and not v_src.startswith("data:"):
                                resolved_stream = v_src
                    except Exception as e:
                        status_dict[ep_num] = {"status": f"Embed Error: {e}", "color": "red", "quality": "-"}
                    finally:
                        try:
                            await player_page.close()
                        except Exception:
                            pass

                    if resolved_stream:
                        break
        elif is_witanime == 0:
            # Anime3rb
            status_dict[ep_num] = {"status": "Extracting source...", "color": "yellow", "quality": "-"}

            # Determine quality order from settings
            cfg = load_config()
            pref_q = cfg.get("default_quality", "auto")
            if pref_q == "720p":
                quality_order = ["720p", "1080p", "480p"]
            elif pref_q == "480p":
                quality_order = ["480p", "720p", "1080p"]
            else:
                quality_order = ["1080p", "720p", "480p"]

            # Wait up to 10 seconds for video inside iframe
            success = False
            for _ in range(10):
                frames = page.frames
                player_frame = next((f for f in frames if "vid3rb.com" in f.url or "player" in f.url), None)
                if player_frame:
                    # Try parsing source variables
                    try:
                        frame_html = await player_frame.content()
                        m = re.search(r'var\s+video_sources\s*=\s*(\[\s*\{[\s\S]*?\}\s*\])\s*;', frame_html)
                        if m:
                            sources = json.loads(m.group(1))
                            for q in quality_order:
                                for s in sources:
                                    if s.get("label") == q and s.get("src") and not s.get("premium"):
                                        resolved_stream = s["src"]
                                        success = True
                                        break
                                if success:
                                    break
                    except Exception:
                        pass

                    if success:
                        break

                    # Fallback to network request capture
                    if media_requests:
                        resolved_stream = media_requests[0]
                        success = True
                        break
                await asyncio.sleep(1.0)

            if not resolved_stream and media_requests:
                resolved_stream = media_requests[0]

    except Exception as e:
        status_dict[ep_num] = {"status": f"Failed: {e}", "color": "red", "quality": "-"}
    finally:
        await page.close()
        await context.close()

    if resolved_stream:
        results_dict[ep_num] = resolved_stream
        quality = "FHD/1080p" if any(q in resolved_stream.lower() for q in ["1080p", "fhd", "w1080p"]) else "HD/720p" if any(q in resolved_stream.lower() for q in ["720p", "hd"]) else "SD/480p" if "480p" in resolved_stream.lower() else "Auto"
        status_dict[ep_num] = {"status": "Resolved ✔", "color": "green", "quality": quality}
    else:
        status_dict[ep_num] = {"status": "Failed ✘", "color": "red", "quality": "-"}


async def scrape_multiple_streams_async(ep_items, is_witanime, active_cookies):
    from playwright.async_api import async_playwright

    results = {}
    status_dict = {}
    for ep in ep_items:
        status_dict[ep["episode"]] = {"status": "Pending...", "color": "gray", "quality": "-"}

    def make_scraping_table():
        table = Table(box=None, show_header=True, border_style=THEME['border'])
        table.add_column("Episode", justify="center", style=f"bold {THEME['primary']}")
        table.add_column("Status", justify="left")
        table.add_column("Quality", justify="center", style=f"bold {THEME['success']}")

        color_map = {
            "gray": THEME['dim'],
            "cyan": THEME['primary'],
            "blue": THEME['accent'],
            "yellow": THEME['warning'],
            "green": THEME['success'],
            "red": THEME['error'],
        }

        for ep_num in sorted(status_dict.keys()):
            info = status_dict[ep_num]
            raw_color = info["color"]
            theme_color = color_map.get(raw_color, THEME['fg'])
            status_text = f"[{theme_color}]{info['status']}[/{theme_color}]"
            table.add_row(f"Episode {ep_num}", status_text, info["quality"])

        resolved_count = sum(1 for info in status_dict.values() if "Resolved" in info["status"])
        failed_count = sum(1 for info in status_dict.values() if "Failed" in info["status"])
        total_count = len(status_dict)
        done_count = resolved_count + failed_count

        pct = int((done_count / total_count) * 100) if total_count > 0 else 0
        bar_len = 20
        filled_len = int(bar_len * done_count // total_count) if total_count > 0 else 0
        bar = "█" * filled_len + "░" * (bar_len - filled_len)

        progress_text = f"\n[bold {THEME['accent']}]Progress:[/bold {THEME['accent']}] [bold {THEME['success']}]{bar}[/bold {THEME['success']}] {pct}%\n"
        progress_text += f"[bold {THEME['success']}]{get_icon('check')}Scraped:[/bold {THEME['success']}] {resolved_count} | [bold {THEME['error']}]{get_icon('cross')}Failed:[/bold {THEME['error']}] {failed_count} | [bold {THEME['primary']}]Total:[/bold {THEME['primary']}] {total_count}"

        progress_panel = Panel(
            progress_text,
            title=f"[bold {THEME['primary']}]Scraping Overview[/bold {THEME['primary']}]",
            border_style=THEME['border'],
            expand=False
        )

        return Columns([
            Panel(table, title=f"[bold {THEME['primary']}]Task Progress[/bold {THEME['primary']}]", border_style=THEME['border'], expand=False),
            progress_panel
        ])

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                msg = str(e)
                if "executable doesn't exist" in msg.lower() or "playwright install" in msg.lower():
                    msg = "Playwright Chromium browser is not installed. Please run 'playwright install' or 'python3 -m playwright install' in your terminal."
                raise RuntimeError(msg) from e

            tasks = []
            for ep in ep_items:
                tasks.append(scrape_one_stream_async(browser, ep, is_witanime, active_cookies, results, status_dict))

            with Live(make_scraping_table(), refresh_per_second=5, transient=False) as live:
                # Update live display periodically while tasks run
                async def update_display():
                    while True:
                        await asyncio.sleep(1.0)
                        live.update(make_scraping_table())

                display_task = asyncio.create_task(update_display())
                try:
                    await asyncio.gather(*tasks)
                finally:
                    display_task.cancel()
                    try:
                        await display_task
                    except asyncio.CancelledError:
                        pass
                live.update(make_scraping_table())

            await browser.close()
    except Exception as e:
        raise e
    return results



# ════════════════════════════════════════════════════════════
#  Main Application (State Machine)
# ════════════════════════════════════════════════════════════

def run_app():
    # stack format: list of dicts, each has "state" and other metadata
    stack = [{"state": "MAIN_MENU"}]

    while stack:
        # Load configuration settings dynamically on each iteration!
        cfg = load_config()
        pref_player = cfg.get("preferred_player", "auto")

        # Discover all players dynamically on each iteration!
        vlc = find_vlc()
        mpv = find_mpv()
        iina = find_iina()
        celluloid = find_celluloid()
        haruna = find_haruna()

        active_player = None
        player_name = "None"

        if pref_player == "vlc":
            if vlc:
                active_player = vlc
                player_name = "VLC"
        elif pref_player == "mpv":
            if mpv:
                active_player = mpv
                player_name = "MPV"
        elif pref_player == "iina":
            if iina:
                active_player = iina
                player_name = "IINA"
        elif pref_player == "celluloid":
            if celluloid:
                active_player = celluloid
                player_name = "Celluloid"
        elif pref_player == "haruna":
            if haruna:
                active_player = haruna
                player_name = "Haruna"
        else:
            # "auto" mode: pick whatever is available, prefer mpv
            if mpv:
                active_player = mpv
                player_name = "MPV"
            elif vlc:
                active_player = vlc
                player_name = "VLC"
            elif iina:
                active_player = iina
                player_name = "IINA"
            elif celluloid:
                active_player = celluloid
                player_name = "Celluloid"
            elif haruna:
                active_player = haruna
                player_name = "Haruna"

        # Clear screen and draw header/logo
        clear_screen()
        print_logo()

        if active_player:
            console.print(f"\n[bold {THEME['success']}]{get_icon('check')}Active Player:[/bold {THEME['success']}] [{THEME['fg']}]{player_name} ({active_player})[/{THEME['fg']}]")
        elif pref_player in ("mpv", "vlc"):
            console.print(f"\n[bold {THEME['warning']}]{get_icon('warning')}Preferred player {pref_player.upper()} not found. Will auto-install when needed.[/bold {THEME['warning']}]")
        else:
            console.print(f"\n[bold {THEME['warning']}]{get_icon('warning')}Neither VLC nor MPV was discovered. Will auto-install when needed.[/bold {THEME['warning']}]")

        current = stack[-1]
        state = current["state"]

        # Set terminal window title based on state
        if state == "MAIN_MENU":
            set_terminal_title("Anime CLI Player")
        elif state == "SEARCH_INPUT":
            set_terminal_title("Search Input")
        elif state == "SEARCH_RESULTS":
            set_terminal_title(f"Search Results: {current.get('query', '')}")
        elif state == "URL_INPUT":
            set_terminal_title("Direct URL Input")
        elif state == "FAVORITES":
            set_terminal_title("Favorites Library")
        elif state == "SETTINGS":
            set_terminal_title("Configuration Settings")
        elif state == "EPISODE_SELECTION":
            set_terminal_title(f"Episodes: {current.get('slug', '')}")
        elif state == "PLAYBACK":
            set_terminal_title(f"Playing: {current.get('slug', '')}")

        try:
            if state == "MAIN_MENU":
                platforms = [
                    f"{get_icon('search')}Search Anime",
                    f"{get_icon('direct_url')}Enter URL Directly",
                    f"{get_icon('favorite_on')}Favorites / Library",
                    f"{get_icon('settings')}Settings / Configuration",
                    f"{get_icon('exit')}Exit"
                ]
                fav_count = 0
                try:
                    conn = sqlite3.connect(get_db_path())
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM favorites")
                    fav_count = cursor.fetchone()[0]
                    conn.close()
                except Exception:
                    pass
                menu_metadata = {
                    "pref_player": cfg.get("preferred_player", "auto"),
                    "default_quality": cfg.get("default_quality", "auto"),
                    "pref_browser": cfg.get("preferred_browser", "auto"),
                    "favorites_count": fav_count,
                    "anilist_linked": get_account_token("anilist") is not None,
                    "mal_linked": get_account_token("myanimelist") is not None
                }
                choice_idx, choice_opt = interactive_select(platforms, "Main Menu", context_type="main_menu", metadata=menu_metadata)
                if choice_idx == 4 or choice_idx == -1:
                    # Exit
                    break

                if choice_idx == 0:
                    stack.append({"state": "SEARCH_INPUT"})
                elif choice_idx == 1:
                    stack.append({"state": "URL_INPUT"})
                elif choice_idx == 2:
                    stack.append({"state": "FAVORITES"})
                elif choice_idx == 3:
                    stack.append({"state": "SETTINGS"})

            elif state == "SEARCH_INPUT":
                cfg = load_config()
                search_hist = cfg.get("search_history", [])

                query = None
                if search_hist:
                    hist_opts = ["[New Search Query]"] + search_hist
                    sel_idx, sel_opt = interactive_select(hist_opts, "Recent Searches")
                    if sel_idx == -1:
                        stack.pop()
                        continue
                    if sel_idx == 0:
                        query = prompt_input("\n[bold blue]❯ Enter search query (or press Enter/Esc to go back): [/bold blue]")
                    else:
                        query = sel_opt
                else:
                    query = prompt_input("\n[bold blue]❯ Enter search query (or press Enter/Esc to go back): [/bold blue]")

                if not query:
                    stack.pop()
                    continue

                # Save to search history
                add_search_history(query)

                clear_screen()
                print_logo()
                with console.status(f"[bold {THEME['primary']}]{get_icon('search')}Searching all sources...[/bold {THEME['primary']}]", spinner="dots"):
                    try:
                        async def perform_unified_search(q):
                            res_3rb, res_wit = await asyncio.gather(
                                search_anime3rb_async(q),
                                search_witanime_async(q),
                                return_exceptions=True
                            )
                            r_3rb = res_3rb if isinstance(res_3rb, list) else []
                            r_wit = res_wit if isinstance(res_wit, list) else []
                            
                            unified = []
                            for title, href in r_3rb:
                                unified.append((f"[Anime3rb] {title}", href, 0))
                            for title, href in r_wit:
                                unified.append((f"[WitAnime] {title}", href, 1))
                            return unified
                            
                        search_results = asyncio.run(perform_unified_search(query))
                    except KeyboardInterrupt:
                        search_results = []
                        print_warn("Search cancelled by user.")
                        time.sleep(1.0)

                if not search_results:
                    print_warn("No search results found. Press any key to continue...")
                    read_key()
                    continue

                stack.append({
                    "state": "SEARCH_RESULTS",
                    "query": query,
                    "search_results": search_results
                })

            elif state == "SEARCH_RESULTS":
                query = current["query"]
                search_results = current["search_results"]

                options = [title for title, _, _ in search_results]
                results_metadata = {
                    "search_query": query
                }
                sel_idx, sel_opt = interactive_select(options, f"Results for '{query}'", context_type="search_results", metadata=results_metadata)
                if sel_idx == -1:
                    stack.pop()
                    continue
                selected_result = search_results[sel_idx]
                anime_url = selected_result[1]
                is_witanime = selected_result[2]
                title_text = selected_result[0]

                # Fetch episodes
                clear_screen()
                print_logo()
                slug = extract_slug(anime_url)
                if not slug:
                    print_fail(f"Could not extract slug from URL: {anime_url}. Press any key...")
                    read_key()
                    continue

                print_info(f"Target URL Slug: {slug}")

                # Sync cookies
                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Syncing cookies from browser profiles...[/bold {THEME['primary']}]", spinner="dots"):
                    active_cookies = get_preferred_cookies()

                if active_cookies:
                    print_ok(f"Synced {len(active_cookies)} cookies. Bypassing Turnstile.")
                else:
                    print_warn("No cookies synced. Using clean session.")

                # Fetch episodes list
                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Loading episodes list...[/bold {THEME['primary']}]", spinner="dots"):
                    try:
                        eps, err = asyncio.run(fetch_episodes_list_async(anime_url, is_witanime, active_cookies))
                    except KeyboardInterrupt:
                        eps, err = [], "Action cancelled."
                    except Exception as exc:
                        eps, err = [], str(exc)

                if err:
                    print_fail(f"Error fetching episodes: {err}. Press any key to return...")
                    read_key()
                    continue

                if not eps:
                    print_warn("No episodes found. Press any key to return...")
                    read_key()
                    continue

                stack.append({
                    "state": "EPISODE_SELECTION",
                    "eps": eps,
                    "slug": slug,
                    "anime_url": anime_url,
                    "is_witanime": is_witanime,
                    "title": title_text,
                    "came_from_search": True,
                    "auto_play": False
                })

            elif state == "URL_INPUT":
                anime_url = prompt_input("\n[bold blue]❯ Enter Anime URL (or press Enter/Esc to go back): [/bold blue]")
                if not anime_url:
                    stack.pop()
                    continue

                p = urlparse(anime_url)
                if "witanime" in p.netloc:
                    is_witanime = 1
                else:
                    is_witanime = 0
                slug = extract_slug(anime_url)
                if not slug:
                    print_fail(f"Could not extract slug from URL: {anime_url}. Press any key...")
                    read_key()
                    continue

                # Fetch episodes
                clear_screen()
                print_logo()
                print_info(f"Target Anime Slug: {slug}")

                # Sync cookies
                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Syncing cookies from browser profiles...[/bold {THEME['primary']}]", spinner="dots"):
                    active_cookies = get_preferred_cookies()

                if active_cookies:
                    print_ok(f"Synced {len(active_cookies)} cookies. Bypassing Turnstile.")
                else:
                    print_warn("No cookies synced. Using clean session.")

                # Fetch episodes list
                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Loading episodes list...[/bold {THEME['primary']}]", spinner="dots"):
                    try:
                        eps, err = asyncio.run(fetch_episodes_list_async(anime_url, is_witanime, active_cookies))
                    except KeyboardInterrupt:
                        eps, err = [], "Action cancelled."
                    except Exception as exc:
                        eps, err = [], str(exc)

                if err:
                    print_fail(f"Error fetching episodes: {err}. Press any key...")
                    read_key()
                    continue

                if not eps:
                    print_warn("No episodes found. Press any key...")
                    read_key()
                    continue

                stack.append({
                    "state": "EPISODE_SELECTION",
                    "eps": eps,
                    "slug": slug,
                    "anime_url": anime_url,
                    "is_witanime": is_witanime,
                    "title": slug,
                    "came_from_search": False,
                    "auto_play": False
                })

            elif state == "FAVORITES":
                db_path = get_db_path()
                favs = []
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT slug, title, url, is_witanime FROM favorites")
                    for row in cursor.fetchall():
                        favs.append({
                            "slug": row[0],
                            "title": row[1],
                            "url": row[2],
                            "is_witanime": int(row[3])
                        })
                    conn.close()
                except Exception:
                    pass

                if not favs:
                    print_warn("No favorites bookmarked yet.")
                    console.print(f"[{THEME['dim']}]Press any key to go back...[/{THEME['dim']}]")
                    read_key()
                    stack.pop()
                    continue

                fav_slugs = [f["slug"] for f in favs]
                favorites_metadata = {
                    "fav_slugs": fav_slugs
                }
                options = [f"{f['title']} ({get_provider_name(f.get('is_witanime', 0))})" for f in favs]
                sel_idx, sel_opt = interactive_select(options, "Bookmarked Anime", context_type="favorites", metadata=favorites_metadata)
                if sel_idx == -1:
                    stack.pop()
                    continue

                selected_fav = favs[sel_idx]
                anime_url = selected_fav["url"]
                is_witanime = int(selected_fav.get("is_witanime", 0))
                slug = selected_fav["slug"]

                # Fetch episodes
                clear_screen()
                print_logo()
                print_info(f"Target Anime Slug: {slug}")

                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Syncing cookies...[/bold {THEME['primary']}]", spinner="dots"):
                    active_cookies = get_preferred_cookies()

                with console.status(f"[bold {THEME['primary']}]{get_icon('watch_history')}Loading episodes list...[/bold {THEME['primary']}]", spinner="dots"):
                    try:
                        eps, err = asyncio.run(fetch_episodes_list_async(anime_url, is_witanime, active_cookies))
                    except KeyboardInterrupt:
                        eps, err = [], "Action cancelled."
                    except Exception as exc:
                        eps, err = [], str(exc)

                if err:
                    print_fail(f"Error fetching episodes: {err}. Press any key to return...")
                    read_key()
                    continue

                if not eps:
                    print_warn("No episodes found. Press any key to return...")
                    read_key()
                    continue

                stack.append({
                    "state": "EPISODE_SELECTION",
                    "eps": eps,
                    "slug": slug,
                    "anime_url": anime_url,
                    "is_witanime": is_witanime,
                    "title": selected_fav["title"],
                    "came_from_search": False,
                    "auto_play": False
                })

            elif state == "SETTINGS":
                cfg = load_config()
                current_player = cfg.get("preferred_player", "auto")
                current_quality = cfg.get("default_quality", "auto")
                current_browser = cfg.get("preferred_browser", "auto")
                history_enabled = cfg.get("history_tracking", True)
                fullscreen_enabled = cfg.get("fullscreen", True)
                player_args = cfg.get("custom_player_args", "")

                # Show current player status
                vlc_status = f"[bold {THEME['success']}]Installed[/bold {THEME['success']}]" if vlc else f"[bold {THEME['error']}]Not Found[/bold {THEME['error']}]"
                mpv_status = f"[bold {THEME['success']}]Installed[/bold {THEME['success']}]" if mpv else f"[bold {THEME['error']}]Not Found[/bold {THEME['error']}]"
                iina_status = f"[bold {THEME['success']}]Installed[/bold {THEME['success']}]" if iina else f"[bold {THEME['error']}]Not Found[/bold {THEME['error']}]"
                celluloid_status = f"[bold {THEME['success']}]Installed[/bold {THEME['success']}]" if celluloid else f"[bold {THEME['error']}]Not Found[/bold {THEME['error']}]"
                haruna_status = f"[bold {THEME['success']}]Installed[/bold {THEME['success']}]" if haruna else f"[bold {THEME['error']}]Not Found[/bold {THEME['error']}]"
                use_nerd = cfg.get("nerd_fonts", False)

                # Render Diagnostics & System Configuration Status
                diag_table = Table(
                    title=f"[bold {THEME['primary']}]⚙️ System Diagnostics & Configuration[/bold {THEME['primary']}]",
                    show_header=True,
                    header_style=f"bold {THEME['accent']}",
                    border_style=THEME['border']
                )
                diag_table.add_column("Setting Name", style=f"bold {THEME['fg']}")
                diag_table.add_column("Value / Status", style=f"{THEME['success']}")

                diag_table.add_row("Preferred Video Player", f"{current_player.upper()} (VLC: {vlc_status}, MPV: {mpv_status}, IINA: {iina_status}, Celluloid: {celluloid_status}, Haruna: {haruna_status})")
                diag_table.add_row("Default Stream Quality", current_quality.upper())
                diag_table.add_row("Cookie Extraction Browser", current_browser.upper())
                diag_table.add_row("Watch History Tracking", "Enabled" if history_enabled else f"Disabled [bold {THEME['warning']}]({get_icon('warning')}Private Mode)[/bold {THEME['warning']}]")
                diag_table.add_row("Auto Fullscreen Mode", "Enabled" if fullscreen_enabled else "Disabled")
                diag_table.add_row("Custom Player Arguments", player_args if player_args else f"[dim {THEME['dim']}]None[/dim {THEME['dim']}]")
                diag_table.add_row("Nerd Font Icons Support", f"Enabled ({get_icon('check')}Active)" if use_nerd else "Disabled (Standard Unicode)")
                
                anilist_info = get_account_token("anilist")
                mal_info = get_account_token("myanimelist")
                al_diag = f"[bold {THEME['success']}]Linked[/bold {THEME['success']}]" if anilist_info else f"[dim {THEME['dim']}]Not Linked[/dim {THEME['dim']}]"
                mal_diag = f"[bold {THEME['success']}]Linked[/bold {THEME['success']}]" if mal_info else f"[dim {THEME['dim']}]Not Linked[/dim {THEME['dim']}]"
                diag_table.add_row("AniList Integration", al_diag)
                diag_table.add_row("MyAnimeList Integration", mal_diag)
                
                diag_table.add_row("Configuration File Path", f"[dim {THEME['dim']}]{get_config_path()}[/dim {THEME['dim']}]")

                console.print(diag_table)
                console.print()

                settings_opts = [
                    f"Preferred Player       (Current: {current_player.upper()})",
                    f"Default Video Quality  (Current: {current_quality.upper()})",
                    f"Cookie Sync Browser    (Current: {current_browser.upper()})",
                    f"History Tracking       (Current: {'ENABLED' if history_enabled else 'DISABLED'})",
                    f"Auto Fullscreen        (Current: {'ENABLED' if fullscreen_enabled else 'DISABLED'})",
                    f"Custom Player Args     (Current: '{player_args if player_args else 'None'}')",
                    f"Nerd Font Icons        (Current: {'ENABLED' if use_nerd else 'DISABLED'})",
                    "Accounts Integration (AniList / MyAnimeList)",
                    "Clear Search History",
                    "Clear All Watch History & Bookmarks",
                    "Go Back"
                ]

                fav_count = 0
                try:
                    conn = sqlite3.connect(get_db_path())
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM favorites")
                    fav_count = cursor.fetchone()[0]
                    conn.close()
                except Exception:
                    pass
                settings_metadata = {
                    "pref_player": cfg.get("preferred_player", "auto"),
                    "default_quality": cfg.get("default_quality", "auto"),
                    "pref_browser": cfg.get("preferred_browser", "auto"),
                    "favorites_count": fav_count,
                    "anilist_linked": get_account_token("anilist") is not None,
                    "mal_linked": get_account_token("myanimelist") is not None
                }
                sel_idx, sel_opt = interactive_select(settings_opts, "Configuration / Settings", context_type="settings", metadata=settings_metadata)
                if sel_idx == -1 or sel_idx == 10:
                    stack.pop()
                    continue

                if sel_idx == 0:
                    # Player selection
                    players = ["auto", "vlc", "mpv", "iina", "celluloid", "haruna"]
                    p_idx, p_opt = interactive_select(players, "Select Preferred Player")
                    if p_idx != -1:
                        cfg["preferred_player"] = p_opt
                        save_config(cfg)
                        console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Preferred player set to: {p_opt.upper()}[/bold {THEME['success']}]")

                        # Offer to download player if not installed
                        is_missing = False
                        if p_opt == "mpv" and not find_mpv():
                            is_missing = True
                        elif p_opt == "vlc" and not find_vlc():
                            is_missing = True
                        elif p_opt == "iina" and not find_iina():
                            is_missing = True
                        elif p_opt == "celluloid" and not find_celluloid():
                            is_missing = True
                        elif p_opt == "haruna" and not find_haruna():
                            is_missing = True

                        if is_missing:
                            console.print(f"\n[bold {THEME['warning']}]" + get_icon("warning") + f"{p_opt.upper()} is not installed on your system.[/bold {THEME['warning']}]")
                            choice = [f"Yes, install {p_opt.upper()} now", "No, install it manually later"]
                            c_idx, _ = interactive_select(choice, f"Would you like to install {p_opt.upper()}?")
                            if c_idx == 0:
                                install_player(p_opt)

                        time.sleep(1.0)

                elif sel_idx == 1:
                    # Quality selection
                    qualities = ["auto", "1080p", "720p", "480p", "360p"]
                    q_idx, q_opt = interactive_select(qualities, "Select Default Quality")
                    if q_idx != -1:
                        cfg["default_quality"] = q_opt
                        save_config(cfg)
                        console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Default quality set to: {q_opt.upper()}[/bold {THEME['success']}]")
                        time.sleep(1.0)

                elif sel_idx == 2:
                    # Browser selection
                    browsers = ["auto", "chrome", "edge"]
                    b_idx, b_opt = interactive_select(browsers, "Select Preferred Cookie Browser")
                    if b_idx != -1:
                        cfg["preferred_browser"] = b_opt
                        save_config(cfg)
                        console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Preferred browser for cookies set to: {b_opt.upper()}[/bold {THEME['success']}]")
                        time.sleep(1.0)

                elif sel_idx == 3:
                    # History tracking toggle
                    cfg["history_tracking"] = not history_enabled
                    save_config(cfg)
                    status_str = "ENABLED" if not history_enabled else "DISABLED"
                    console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Watch history tracking set to: {status_str}[/bold {THEME['success']}]")
                    time.sleep(1.0)

                elif sel_idx == 4:
                    # Fullscreen toggle
                    cfg["fullscreen"] = not fullscreen_enabled
                    save_config(cfg)
                    status_str = "ENABLED" if not fullscreen_enabled else "DISABLED"
                    console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Auto Fullscreen set to: {status_str}[/bold {THEME['success']}]")
                    time.sleep(1.0)

                elif sel_idx == 5:
                    # Custom Player Args
                    console.print(f"\n[bold {THEME['primary']}]" + get_icon("settings") + "Custom Player Arguments[/bold {THEME['primary']}]")
                    console.print(f"[dim {THEME['dim']}]Enter custom command-line arguments to pass to the player (e.g. --fs --volume=80).[/dim {THEME['dim']}]")
                    console.print(f"[dim {THEME['dim']}]Press Enter with empty input to clear custom arguments.[/dim {THEME['dim']}]")
                    new_args = prompt_input(f"❯ New player arguments (Current: '{player_args}'): ")
                    if new_args is not None:
                        cfg["custom_player_args"] = new_args.strip()
                        save_config(cfg)
                        console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + "Custom player arguments updated![/bold {THEME['success']}]")
                        time.sleep(1.0)

                elif sel_idx == 6:
                    # Nerd Fonts toggle
                    cfg["nerd_fonts"] = not use_nerd
                    save_config(cfg)
                    status_str = "ENABLED" if not use_nerd else "DISABLED"
                    console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + f"Nerd Font Icons support set to: {status_str}[/bold {THEME['success']}]")
                    time.sleep(1.0)

                elif sel_idx == 7:
                    # Accounts integration sub-menu
                    while True:
                        anilist_info = get_account_token("anilist")
                        mal_info = get_account_token("myanimelist")
                        
                        al_status = f"[bold green]Linked[/bold green]" if anilist_info else "[dim]Not Linked[/dim]"
                        mal_status = f"[bold green]Linked[/bold green]" if mal_info else "[dim]Not Linked[/dim]"
                        
                        acc_opts = [
                            f"Link AniList Account       (Status: {al_status})",
                            f"Link MyAnimeList Account   (Status: {mal_status})",
                            "Unlink AniList Account" if anilist_info else "Unlink AniList Account (Disabled)",
                            "Unlink MyAnimeList Account" if mal_info else "Unlink MyAnimeList Account (Disabled)",
                            "Go Back"
                        ]
                        
                        sub_idx, _ = interactive_select(acc_opts, "Accounts Integration")
                        if sub_idx == -1 or sub_idx == 4:
                            break
                        
                        if sub_idx == 0:
                            link_anilist_flow()
                        elif sub_idx == 1:
                            link_myanimelist_flow()
                        elif sub_idx == 2:
                            if anilist_info:
                                remove_account("anilist")
                                console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + "Unlinked AniList account.[/bold {THEME['success']}]")
                                time.sleep(1.0)
                        elif sub_idx == 3:
                            if mal_info:
                                remove_account("myanimelist")
                                console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + "Unlinked MyAnimeList account.[/bold {THEME['success']}]")
                                time.sleep(1.0)

                elif sel_idx == 8:
                    cfg["search_history"] = []
                    save_config(cfg)
                    console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + "Search history cleared![/bold {THEME['success']}]")
                    time.sleep(1.0)

                elif sel_idx == 9:
                    try:
                        conn = sqlite3.connect(get_db_path())
                        cursor = conn.cursor()
                        cursor.execute("DELETE FROM favorites")
                        cursor.execute("DELETE FROM shows")
                        cursor.execute("DELETE FROM watched_episodes")
                        cursor.execute("DELETE FROM episode_progress")
                        cursor.execute("DELETE FROM accounts")
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
                    cfg["history"] = {}
                    cfg["favorites"] = []
                    save_config(cfg)
                    console.print(f"\n[bold {THEME['success']}]" + get_icon("check") + "Watch history, bookmarks, and linked accounts cleared![/bold {THEME['success']}]")
                    time.sleep(1.0)

            elif state == "EPISODE_SELECTION":
                eps = current["eps"]
                slug = current["slug"]
                anime_url = current["anime_url"]
                is_witanime = current["is_witanime"]
                anime_title = current.get("title", slug)

                # Fetch watch history
                history_data = get_watch_history(slug)
                last_watched = history_data.get("last_watched", 0)
                watched_list = history_data.get("watched", [])

                # Determine default cursor index (suggest next episode if last_watched was found)
                last_watched_idx = next((i for i, x in enumerate(eps) if x['episode'] == last_watched), -1)
                default_idx = 0
                if last_watched_idx != -1:
                    if last_watched_idx + 1 < len(eps):
                        default_idx = last_watched_idx + 1
                    else:
                        default_idx = last_watched_idx

                # Build option text with watch indicators
                ep_options = []
                for x in eps:
                    ep_num = x['episode']
                    if ep_num in watched_list:
                        ep_options.append(f"Episode {ep_num} [dim {THEME['dim']}](watched {get_icon('check').strip()})[/dim {THEME['dim']}]")
                    else:
                        ep_options.append(f"Episode {ep_num}")

                # Check favorite status
                fav_status = is_favorite_slug(slug)

                def on_toggle_fav():
                    return toggle_favorite_state(anime_title, anime_url, is_witanime, slug)

                if current.get("auto_play", False) and len(eps) == 1:
                    selected_indices = [0]
                else:
                    ep_metadata = {
                        "anime_title": anime_title,
                        "provider": get_provider_name(is_witanime),
                        "player_name": player_name
                    }
                    selected_indices = interactive_checklist(
                        ep_options,
                        title=f"Select episodes ({slug})",
                        default_start_idx=default_idx,
                        is_favorite=fav_status,
                        on_toggle_favorite=on_toggle_fav,
                        context_type="episode_selection",
                        metadata=ep_metadata
                    )
                if not selected_indices:
                    stack.pop()
                    continue

                eps_to_scrape = [eps[i] for i in selected_indices]
                ep_numbers = [ep["episode"] for ep in eps_to_scrape]

                # Scrape
                clear_screen()
                print_logo()
                console.print(f"[bold {THEME['primary']}]{get_icon('watch_history')}Scraping stream URLs for {len(eps_to_scrape)} episodes concurrently...[/bold {THEME['primary']}]")
                active_cookies = get_preferred_cookies()

                try:
                    results = asyncio.run(scrape_multiple_streams_async(eps_to_scrape, is_witanime, active_cookies))
                except KeyboardInterrupt:
                    print_warn("Scraping cancelled by user. Press any key...")
                    read_key()
                    continue
                except Exception as exc:
                    print_fail(f"Scraping engine error: {exc}. Press any key...")
                    read_key()
                    continue

                stream_urls = []
                for ep in eps_to_scrape:
                    ep_num = ep["episode"]
                    u_str = results.get(ep_num)
                    if u_str:
                        stream_urls.append(u_str)

                if not stream_urls:
                    print_fail("No stream URLs resolved. Press any key...")
                    read_key()
                    continue

                # ── PLAYBACK: Auto-install player if needed ──
                if not active_player:
                    # Determine which player to install based on preference
                    target_to_install = pref_player if pref_player in ("mpv", "vlc", "iina", "celluloid", "haruna") else "mpv"
                    console.print(f"\n[bold {THEME['warning']}]{get_icon('warning')}Preferred player {target_to_install.upper()} is not installed on your system.[/bold {THEME['warning']}]")
                    console.print(f"[bold {THEME['primary']}]{get_icon('watch_history')}Automatically downloading and installing {target_to_install.upper()} now...[/bold {THEME['primary']}]")
                    time.sleep(1.0)

                    success_install = install_player(target_to_install)
                    if success_install:
                        clear_player_cache()
                        # Re-read paths and set active player
                        vlc = find_vlc()
                        mpv = find_mpv()
                        iina = find_iina()
                        celluloid = find_celluloid()
                        haruna = find_haruna()

                        if target_to_install == "mpv" and mpv:
                            active_player = mpv
                            player_name = "MPV"
                        elif target_to_install == "vlc" and vlc:
                            active_player = vlc
                            player_name = "VLC"
                        elif target_to_install == "iina" and iina:
                            active_player = iina
                            player_name = "IINA"
                        elif target_to_install == "celluloid" and celluloid:
                            active_player = celluloid
                            player_name = "Celluloid"
                        elif target_to_install == "haruna" and haruna:
                            active_player = haruna
                            player_name = "Haruna"

                    # Fallback if preferred installation failed: scan for any other player
                    if not active_player:
                        for p_name, find_func, display_name in [
                            ("mpv", find_mpv, "MPV"),
                            ("vlc", find_vlc, "VLC"),
                            ("iina", find_iina, "IINA"),
                            ("celluloid", find_celluloid, "Celluloid"),
                            ("haruna", find_haruna, "Haruna"),
                        ]:
                            p_path = find_func()
                            if p_path:
                                console.print(f"\n[bold {THEME['warning']}]{get_icon('warning')}{target_to_install.upper()} installation failed. Falling back to {display_name}.[/bold {THEME['warning']}]")
                                active_player = p_path
                                player_name = display_name
                                time.sleep(2.0)
                                break

                    # Last resort fallback: install default system player
                    if not active_player:
                        default_fallback = "mpv"
                        if sys.platform == "darwin":
                            default_fallback = "iina"
                        
                        console.print(f"\n[bold {THEME['primary']}]{get_icon('watch_history')}Trying to install {default_fallback.upper()} as fallback...[/bold {THEME['primary']}]")
                        if install_player(default_fallback):
                            vlc = find_vlc()
                            mpv = find_mpv()
                            iina = find_iina()
                            celluloid = find_celluloid()
                            haruna = find_haruna()
                            
                            p_path = find_iina() if default_fallback == "iina" else find_mpv()
                            if p_path:
                                active_player = p_path
                                player_name = default_fallback.upper()

                    # If still no player is found, ask the user how to proceed
                    if not active_player:
                        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Player installation failed and no fallback player is available.[/bold {THEME['error']}]")
                        choice = ["Show Streaming Links Only", "Go Back"]
                        c_idx, _ = interactive_select(choice, "How would you like to proceed?")
                        if c_idx == 1 or c_idx == -1:
                            continue

                # Launch player or show links
                if active_player:
                    print_hotkey_guide(player_name)
                    console.print(f"\n[bold {THEME['success']}]{get_icon('play')}Launching {player_name} with {len(stream_urls)} stream(s)...[/bold {THEME['success']}]")
                    if player_name == "MPV":
                        launch_success = play_with_mpv(stream_urls, slug=slug, ep=eps_to_scrape[0]["episode"])
                    elif player_name == "VLC":
                        launch_success = play_with_vlc(stream_urls)
                    elif player_name == "IINA":
                        launch_success = play_with_iina(stream_urls)
                    elif player_name == "Celluloid":
                        launch_success = play_with_celluloid(stream_urls)
                    elif player_name == "Haruna":
                        launch_success = play_with_haruna(stream_urls)

                    if launch_success:
                        # Record watch history
                        for ep_num in ep_numbers:
                            add_watch_history(slug, ep_num, anime_title)
                        console.print(f"[bold {THEME['success']}]{get_icon('check')}Playback started! {len(stream_urls)} episode(s) queued in {player_name}.[/bold {THEME['success']}]")
                    else:
                        console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Failed to launch {player_name}. Showing links instead:[/bold {THEME['error']}]")
                        for i, s_url in enumerate(stream_urls):
                            console.print(f"  [bold {THEME['fg']}]{i + 1}.[/bold {THEME['fg']}] [{THEME['accent']}]{s_url}[/{THEME['accent']}]")
                else:
                    # No player available — show streaming links
                    console.print(f"\n[bold {THEME['primary']}]═══ Streaming Links ═══[/bold {THEME['primary']}]")
                    for i, s_url in enumerate(stream_urls):
                        console.print(f"  [bold {THEME['fg']}]{i + 1}.[/bold {THEME['fg']}] [{THEME['accent']}]{s_url}[/{THEME['accent']}]")

                # Post-playback prompt
                console.print(f"\n[{THEME['dim']}]Press any key to continue...[/{THEME['dim']}]")
                read_key()

        except KeyboardInterrupt:
            # Allow Ctrl+C to go back one level
            if len(stack) > 1:
                stack.pop()
            else:
                break
        except Exception as exc:
            console.print(f"\n[bold {THEME['error']}]{get_icon('cross')}Unexpected error: {exc}[/bold {THEME['error']}]")
            import traceback
            traceback.print_exc()
            console.print(f"[{THEME['dim']}]Press any key to continue...[/{THEME['dim']}]")
            read_key()
            if len(stack) > 1:
                stack.pop()
            else:
                break



# ════════════════════════════════════════════════════════════
#  Entry Point
# ════════════════════════════════════════════════════════════

def main():
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    # Initialize SQLite database and migrate old data
    init_db()
    migrate_json_to_sqlite()

    # Enable ANSI escape sequences on Windows
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    enter_alt_screen()
    try:
        run_app()
    except Exception as e:
        exit_alt_screen()
        import traceback
        traceback.print_exc()
        input("\nAn unexpected error occurred. Press Enter to exit...")
    finally:
        exit_alt_screen()


if __name__ == "__main__":
    main()