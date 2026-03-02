#!/usr/bin/env python3
"""
mathboxAI - Interactive 3D Math Visualizer with AI Chat

Usage:
    mathboxAI                        Launch empty viewer
    mathboxAI scene.json             Launch with a scene file
    mathboxAI --port 9000            Use custom port
"""

import sys
import json
import os
import http.server
import socketserver
import webbrowser
from pathlib import Path
import threading
import time
import signal
import subprocess
import argparse
import tty
import termios
import select
from urllib.parse import urlparse, parse_qs, quote
from google import genai
from google.genai import types

script_dir = Path(__file__).parent.resolve()
scenes_dir = script_dir / "scenes"

try:
    from gemini_live_tools import GeminiLiveAPI, pcm_to_wav_bytes
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
app_js_path = script_dir / "app.js"
chat_js_path = script_dir / "chat.js"

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL   = os.environ.get('GEMINI_MODEL', 'gemini-3-flash-preview')

DEFAULT_PORT = 8785

# ---------------------------------------------------------------------------
# Agent session memory — persists across turns within one server session.
# Stores eval_math results (and anything else) under agent-chosen keys.
# Cleared on server start; agents control what's stored via store_as param.
# ---------------------------------------------------------------------------
_agent_memory: dict = {}


def _memory_summary(key: str, value) -> str:
    """Human-readable one-liner describing a stored value."""
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            return f"list of {len(value)} lists (e.g. {len(value[0])}-element)"
        return f"list [{len(value)} items]"
    if isinstance(value, (int, float)):
        return f"scalar {value}"
    return str(type(value).__name__)


def _resolve_memory_refs(obj):
    """Recursively replace '$key' strings with values from _agent_memory."""
    if isinstance(obj, str) and obj.startswith('$'):
        key = obj[1:]
        if key in _agent_memory:
            return _agent_memory[key]
        return obj  # unknown key — leave as-is
    if isinstance(obj, dict):
        return {k: _resolve_memory_refs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_memory_refs(item) for item in obj]
    return obj


def kill_server_on_port(port):
    """Kill any process using the specified port."""
    try:
        result = subprocess.run(
            ['lsof', '-ti', f':{port}'],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    print(f"Stopped previous server (PID: {pid})")
                except (ProcessLookupError, ValueError):
                    pass
            elapsed = 0
            while elapsed < 3:
                check = subprocess.run(['lsof', '-ti', f':{port}'],
                                       capture_output=True, text=True)
                if not check.stdout.strip():
                    break
                time.sleep(0.1)
                elapsed += 0.1
    except Exception:
        pass


def list_builtin_scenes():
    """Return list of built-in scene names."""
    if not scenes_dir.exists():
        return []
    return sorted([
        f.stem for f in scenes_dir.glob("*.json")
    ])


def load_builtin_scene(name):
    """Load a built-in scene JSON by name."""
    path = scenes_dir / f"{name}.json"
    if path.exists():
        with open(path, 'r') as f:
            return json.load(f)
    return None


def resolve_scene_path(scene_arg):
    """Resolve scene path from CLI/API input."""
    if not scene_arg:
        return None
    raw = str(scene_arg)
    candidate = Path(raw).expanduser()
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates = [Path.cwd() / candidate, script_dir / candidate]
    for path in candidates:
        if path.exists() and path.is_file():
            return path.resolve()
    return None


FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="15" fill="#1a1a2e"/>
<line x1="20" y1="75" x2="80" y2="75" stroke="#ff4444" stroke-width="4"/>
<line x1="20" y1="75" x2="20" y2="15" stroke="#44ff44" stroke-width="4"/>
<line x1="20" y1="75" x2="55" y2="50" stroke="#4488ff" stroke-width="4"/>
<line x1="20" y1="75" x2="70" y2="30" stroke="#ffaa00" stroke-width="5" stroke-linecap="round"/>
<circle cx="70" cy="30" r="4" fill="#ffaa00"/>
</svg>'''


def generate_html(debug=False):
    """Generate slim HTML shell - all JS logic lives in app.js."""
    debug_mode_js = "true" if debug else "false"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MathBoxAI</title>
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<script src="https://cdn.jsdelivr.net/npm/three@0.137.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.137.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.137.0/examples/js/controls/TrackballControls.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/mathbox@2.3.1/build/mathbox.css">
<script src="https://cdn.jsdelivr.net/npm/mathbox@2.3.1/build/bundle/mathbox.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<div id="app">
    <div id="title-bar">
        <div id="title-content">
            <div id="scene-title-row">
                <div id="scene-title">MathBoxAI</div>
                <span id="scene-source-file" title="">- no file</span>
            </div>
        </div>
        <div id="toolbar">
            <button class="tb-btn" id="btn-load" title="Load JSON file">Load JSON</button>
            <div id="scenes-dropdown">
                <button class="tb-btn" id="btn-scenes">Built-in Scenes &#9662;</button>
                <div id="scenes-menu"></div>
            </div>
            <button class="tb-btn" id="btn-show-json" title="Show current scene JSON">{{ }}</button>
            <button class="tb-btn" id="btn-export-video" title="Record current tab video with TTS audio">Export Video</button>
        </div>
    </div>
    <div id="main-content">
        <div id="scene-dock">
            <div id="scene-dock-toggle" title="Scene tree (T)">&#9776;</div>
            <div id="scene-dock-panel">
                <div id="scene-tree"></div>
            </div>
        </div>
        <div id="viewport">
            <div id="mathbox-container"></div>
            <div id="camera-buttons"></div>
            <div id="viewport-top-left">
                <div id="settings-toggle" title="Display settings">&#9881;</div>
                <div id="projection-toggle">
                    <button class="proj-btn active" data-proj="perspective" title="Perspective projection">Perspective</button>
                    <button class="proj-btn" data-proj="orthographic" title="Orthographic projection">Orthographic</button>
                </div>
            </div>
            <div id="labels-container"></div>
            <div id="legend" class="hidden"></div>
            <div id="follow-angle-lock-toggle" title="Toggle angle-lock for follow camera">&#10227;</div>
            <div id="explain-toggle" title="Toggle explanation panel (E)">&#9776;</div>
            <div id="settings-panel" class="hidden">
                <div class="sp-header"><span></span><span class="sp-col-label">Size</span><span class="sp-col-label">Opacity</span></div>
                <div class="sp-row"><span>Labels</span><span class="sp-ctrl"><button class="sp-btn" data-param="labelScale" data-dir="-">&minus;</button><span class="sp-val" id="val-labelScale">1.0</span><button class="sp-btn" data-param="labelScale" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="labelOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-labelOpacity">1.0</span><button class="sp-btn" data-param="labelOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Arrows</span><span class="sp-ctrl"><button class="sp-btn" data-param="arrowScale" data-dir="-">&minus;</button><span class="sp-val" id="val-arrowScale">1.0</span><button class="sp-btn" data-param="arrowScale" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="arrowOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-arrowOpacity">1.0</span><button class="sp-btn" data-param="arrowOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Axes</span><span class="sp-ctrl"><button class="sp-btn" data-param="axisWidth" data-dir="-">&minus;</button><span class="sp-val" id="val-axisWidth">1.0</span><button class="sp-btn" data-param="axisWidth" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="axisOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-axisOpacity">1.0</span><button class="sp-btn" data-param="axisOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Vectors</span><span class="sp-ctrl"><button class="sp-btn" data-param="vectorWidth" data-dir="-">&minus;</button><span class="sp-val" id="val-vectorWidth">1.0</span><button class="sp-btn" data-param="vectorWidth" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="vectorOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-vectorOpacity">1.0</span><button class="sp-btn" data-param="vectorOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Lines</span><span class="sp-ctrl"><button class="sp-btn" data-param="lineWidth" data-dir="-">&minus;</button><span class="sp-val" id="val-lineWidth">1.0</span><button class="sp-btn" data-param="lineWidth" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="lineOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-lineOpacity">1.0</span><button class="sp-btn" data-param="lineOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Planes</span><span class="sp-ctrl"><button class="sp-btn" data-param="planeScale" data-dir="-">&minus;</button><span class="sp-val" id="val-planeScale">1.0</span><button class="sp-btn" data-param="planeScale" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="planeOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-planeOpacity">0.2</span><button class="sp-btn" data-param="planeOpacity" data-dir="+">+</button></span></div>
                <div class="sp-row"><span>Caption</span><span class="sp-ctrl"><button class="sp-btn" data-param="captionScale" data-dir="-">&minus;</button><span class="sp-val" id="val-captionScale">1.0</span><button class="sp-btn" data-param="captionScale" data-dir="+">+</button></span><span class="sp-ctrl"><button class="sp-btn" data-param="overlayOpacity" data-dir="-">&minus;</button><span class="sp-val" id="val-overlayOpacity">0.7</span><button class="sp-btn" data-param="overlayOpacity" data-dir="+">+</button></span></div>
                <div class="sp-divider"></div>
                <div class="sp-section-label">Light</div>
                <div class="sp-light-row"><span>Azimuth</span><input type="range" id="light-az" min="0" max="360" value="35" class="sp-light-slider"><span class="sp-light-val" id="val-light-az">35°</span></div>
                <div class="sp-light-row"><span>Elevation</span><input type="range" id="light-el" min="10" max="90" value="50" class="sp-light-slider"><span class="sp-light-val" id="val-light-el">50°</span></div>
                <div class="sp-light-row"><span>Brightness</span><input type="range" id="light-int" min="10" max="250" value="80" class="sp-light-slider"><span class="sp-light-val" id="val-light-int">0.80</span></div>
                <div class="sp-divider"></div>
                <div class="sp-section-label">Controls</div>
                <div class="sp-light-row"><span>Momentum</span><input type="range" id="momentum-slider" min="0" max="100" value="50" class="sp-light-slider"><span class="sp-light-val" id="val-momentum">50%</span></div>
                <div class="sp-help">
                    <div>Drag: rotate</div>
                    <div>Shift+drag: pan</div>
                    <div>Wheel / pinch: zoom</div>
                    <div>&#8997;+drag: roll</div>
                </div>
            </div>
            <div id="controls-hint">Drag: rotate &middot; Shift+drag: pan &middot; Zoom: pinch/wheel &middot; &#8997;+drag: roll</div>
            <div id="drop-overlay">Drop JSON file here</div>
            <div id="empty-state">
                <h2>MathBoxAI</h2>
                <p>Drag &amp; drop a scene JSON, or use the toolbar above</p>
            </div>
            <div id="step-caption" class="hidden"></div>
            <div id="scene-description"></div>
            <div id="info-overlays"></div>
            <div id="slider-overlay" class="hidden"></div>
            <div id="scene-nav">
                <button id="nav-prev" class="nav-btn" title="Previous (&#8593;)">&lsaquo;</button>
                <button id="nav-play" class="nav-btn" title="Play (Space)">&#9654;</button>
                <button id="nav-next" class="nav-btn" title="Next (&#8595;)">&rsaquo;</button>
            </div>
        </div>
        <div id="panel-resize-handle"></div>
        <div id="explanation-panel" class="hidden">
            <div id="panel-tabs">
                <button class="panel-tab active" data-tab="doc">Doc</button>
                <button class="panel-tab" data-tab="chat">Chat</button>
            </div>
            <div id="tab-doc" class="tab-content active">
                <div id="doc-toolbar">
                    <button id="doc-speak-btn" title="Read the document aloud">🔊 Speak</button>
                    <button id="doc-commentate-btn" title="AI commentary on this document">💬 Commentate</button>
                </div>
                <div id="explanation-content"></div>
            </div>
            <div id="tab-chat" class="tab-content">
                <div id="chat-tts-controls">
                    <div class="style-picker">
                        <button id="chatCharacterBtn" class="chat-control-btn style-btn" title="Voice character (Ctrl/Cmd+K)">Character</button>
                        <div id="chatCharacterPalette" class="style-palette" hidden>
                            <input id="chatCharacterSearch" class="style-search" type="text" placeholder="Search characters..." />
                            <div id="chatCharacterList" class="style-list"></div>
                        </div>
                    </div>
                    <select id="chatVoiceSelect" class="chat-control-btn" title="Gemini voice"></select>
                    <select id="chatTtsModeSelect" class="chat-control-btn" title="Read: verbatim · Perform: character interprets first · Silent: no TTS">
                        <option value="read">Read</option>
                        <option value="perform">Perform</option>
                        <option value="silent">Silent</option>
                    </select>
                </div>
                <div id="chat-unavailable-msg" class="hidden">
                    <p>AI chat requires a Gemini API key.</p>
                    <p>Set the <code>GEMINI_API_KEY</code> environment variable and restart the server to enable this feature.</p>
                </div>
                <div id="chat-messages"></div>
                <div id="preset-prompts" class="hidden"></div>
                <div id="chat-input-area">
                    <textarea id="chat-input" placeholder="Ask about this visualization..." rows="1"></textarea>
                    <button id="chat-send" title="Send">&#10148;</button>
                </div>
            </div>
        </div>
    </div>
    <div id="chatCharacterBackdrop" class="style-backdrop" hidden></div>
    <div id="status-bar">
        <span id="slider-status" class="hidden">
            <span class="slider-status-icon">🎚️</span><span class="slider-status-count"></span>
            <div class="slider-status-tooltip"></div>
        </span>
        <span id="memory-status" class="hidden">
            <span class="memory-status-icon">💾</span><span class="memory-status-count"></span>
        </span>
        <span id="cam-status">
            <span class="cam-status-icon">📷</span>
            <div class="cam-status-popup" id="cam-popup-content"></div>
        </span>
        <span id="debug-status-text"></span>
    </div>

    <!-- Memory popup -->
    <div id="memory-popup" class="hidden">
        <div id="memory-popup-header">
            <span>Agent Memory</span>
            <button id="memory-popup-close">&times;</button>
        </div>
        <div id="memory-popup-search-wrap">
            <input id="memory-popup-search" type="text" placeholder="Search keys or values..." autocomplete="off">
        </div>
        <div id="memory-popup-body"></div>
    </div>
</div>
<input type="file" id="file-input" accept=".json">
<script>const DEBUG_MODE = {debug_mode_js};</script>
<script src="/app.js"></script>
<script src="/shared/voice-character-selector.js"></script>
<script src="/chat.js"></script>
<div id="json-viewer-overlay" class="hidden">
    <div id="json-viewer-header">
        <span>Scene JSON</span>
        <button id="json-viewer-copy" title="Copy to clipboard">Copy</button>
        <button id="json-viewer-close" title="Close">&times;</button>
    </div>
    <pre id="json-viewer-content"></pre>
</div>
</body>
</html>'''


CSS_CONTENT = '''* { margin: 0; padding: 0; box-sizing: border-box; }

/* Custom scrollbar styling */
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
    background: rgba(100, 100, 200, 0.25);
    border-radius: 3px;
}
::-webkit-scrollbar-thumb:hover { background: rgba(120, 120, 220, 0.4); }
::-webkit-scrollbar-corner { background: transparent; }

/* Firefox scrollbar */
* {
    scrollbar-width: thin;
    scrollbar-color: rgba(100, 100, 200, 0.25) transparent;
}

html, body {
    width: 100%; height: 100%;
    overflow: hidden;
    background: #0a0a0f;
    color: #e0e0e0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

#app {
    display: flex;
    flex-direction: column;
    width: 100%; height: 100%;
}

/* Title Bar */
#title-bar {
    background: rgba(15, 15, 25, 0.95);
    border-bottom: 1px solid rgba(100, 100, 255, 0.15);
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 56px;
    flex-shrink: 0;
    z-index: 100;
}

#title-content {
    flex: 1;
    min-width: 0;
}

#scene-title {
    font-size: 1.3em;
    font-weight: 600;
    color: #ffffff;
    margin-bottom: 2px;
    line-height: 1.3;
}
#scene-title-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
}
#scene-source-file {
    display: inline-block;
    font-size: 11px;
    color: rgba(196, 206, 245, 0.86);
    max-width: 320px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: default;
}

#scene-title .katex { font-size: 1em; }

#scene-description {
    position: absolute;
    bottom: 64px;
    left: 50%;
    transform: translateX(-50%);
    max-width: 70%;
    background: rgba(10, 10, 30, 0.45);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(100, 100, 255, 0.12);
    border-radius: 10px;
    padding: 8px 40px 8px 16px;
    color: rgba(200, 200, 220, 0.75);
    font-size: 0.85em;
    line-height: 1.45;
    text-align: center;
    z-index: 58;
    pointer-events: auto;
    transition: opacity 0.3s ease;
    cursor: grab;
    user-select: none;
}
#scene-description.dragging { cursor: grabbing; }
#scene-description:empty { display: none; }
/* Hide scene description when step caption is active — step caption takes over */
#step-caption:not(.hidden) ~ #scene-description { opacity: 0; pointer-events: none; }
#scene-description .katex { font-size: 0.95em; }

#toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-shrink: 0;
    margin-left: 16px;
}

.tb-btn {
    background: rgba(80, 80, 160, 0.25);
    border: 1px solid rgba(100, 100, 255, 0.25);
    color: #c0c0e0;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 0.82em;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
}

.tb-btn:hover {
    background: rgba(80, 80, 200, 0.4);
    border-color: rgba(120, 120, 255, 0.5);
    color: #fff;
}

.tb-btn.active {
    background: rgba(45, 125, 85, 0.45);
    border-color: rgba(90, 220, 160, 0.6);
    color: #eafff1;
}

#btn-show-json {
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-weight: bold;
    letter-spacing: 1px;
}

/* JSON Viewer Overlay */
#json-viewer-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    z-index: 10000;
    background: rgba(5, 5, 20, 0.85);
    display: flex;
    flex-direction: column;
    padding: 24px;
    backdrop-filter: blur(6px);
}
#json-viewer-overlay.hidden { display: none; }
#json-viewer-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    color: #dde;
    font-size: 1.1em;
    font-weight: 600;
}
#json-viewer-header span { flex: 1; }
#json-viewer-copy, #json-viewer-close {
    background: rgba(80, 80, 160, 0.3);
    border: 1px solid rgba(100, 100, 255, 0.3);
    color: #c0c0e0;
    padding: 4px 14px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 0.9em;
    transition: background 0.15s;
}
#json-viewer-copy:hover, #json-viewer-close:hover {
    background: rgba(80, 80, 200, 0.5);
    color: #fff;
}
#json-viewer-content {
    flex: 1;
    overflow: auto;
    margin: 0;
    padding: 16px;
    background: rgba(10, 10, 30, 0.7);
    border: 1px solid rgba(80, 100, 160, 0.3);
    border-radius: 8px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.88em;
    line-height: 1.5;
    color: rgba(180, 195, 230, 0.9);
    white-space: pre-wrap;
    word-break: break-word;
}

#scenes-dropdown { position: relative; }

#scenes-menu {
    display: none;
    position: absolute;
    top: 100%;
    right: 0;
    margin-top: 4px;
    background: rgba(20, 20, 40, 0.98);
    border: 1px solid rgba(100, 100, 255, 0.3);
    border-radius: 8px;
    padding: 4px 0;
    min-width: 200px;
    max-height: 300px;
    overflow-y: auto;
    z-index: 200;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
}

#scenes-menu.open { display: block; }

.scene-item {
    padding: 8px 16px;
    cursor: pointer;
    font-size: 0.85em;
    color: #c0c0e0;
    transition: background 0.15s;
}

.scene-item:hover {
    background: rgba(80, 80, 200, 0.3);
    color: #fff;
}

/* Main Content */
#main-content {
    display: flex;
    flex: 1;
    overflow: hidden;
}

/* Viewport */
#viewport {
    flex: 1;
    position: relative;
    overflow: hidden;
}

#viewport canvas { display: block; }

#mathbox-container { width: 100%; height: 100%; cursor: grab; }
#mathbox-container canvas { cursor: grab; }
/* Closed-fist grabbing cursor while rotating — overrides MathBox cursor plugin */
body.rotating, body.rotating * { cursor: grabbing !important; }

/* Camera Buttons */
#camera-buttons {
    position: absolute;
    top: 44px;
    right: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    z-index: 50;
}

.cam-btn {
    background: rgba(15, 15, 30, 0.75);
    border: 1px solid rgba(100, 100, 255, 0.2);
    color: #a0a0d0;
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 0.72em;
    cursor: pointer;
    transition: all 0.2s;
    text-align: center;
    min-width: 50px;
    backdrop-filter: blur(8px);
}

.cam-btn:hover {
    background: rgba(60, 60, 160, 0.5);
    color: #fff;
    border-color: rgba(120, 120, 255, 0.5);
}

.cam-btn.active {
    background: rgba(80, 80, 200, 0.5);
    color: #fff;
    border-color: rgba(140, 140, 255, 0.6);
}

.cam-btn-follow {
    border-color: rgba(100, 200, 100, 0.35);
    color: #90d090;
}

.cam-btn-follow:hover {
    background: rgba(30, 100, 50, 0.5);
    border-color: rgba(80, 200, 100, 0.6);
    color: #c0ffc0;
}

.cam-btn-follow.active {
    background: rgba(20, 120, 50, 0.6);
    border-color: rgba(60, 220, 100, 0.7);
    color: #c0ffc0;
    animation: followCamPulse 1.5s ease-in-out infinite;
}

@keyframes followCamPulse {
    0%, 100% { border-color: rgba(60, 220, 100, 0.7); }
    50% { border-color: rgba(60, 220, 100, 0.25); }
}

/* Projection Toggle */
#projection-toggle {
    display: flex;
    gap: 2px;
}
.proj-btn {
    background: rgba(15, 15, 30, 0.75);
    border: 1px solid rgba(100, 100, 255, 0.2);
    color: #a0a0d0;
    padding: 4px 8px;
    font-size: 0.68em;
    cursor: pointer;
    transition: all 0.2s;
    backdrop-filter: blur(8px);
}
.proj-btn:first-child { border-radius: 4px 0 0 4px; }
.proj-btn:last-child { border-radius: 0 4px 4px 0; }
.proj-btn:hover { background: rgba(60, 60, 160, 0.5); color: #fff; }
.proj-btn.active { background: rgba(80, 80, 200, 0.5); color: #fff; border-color: rgba(140, 140, 255, 0.6); }

/* Labels Overlay */
#labels-container {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    pointer-events: none;
    z-index: 40;
}

.label-3d {
    position: absolute;
    top: 0;
    left: 0;
    color: #ffffff;
    font-size: 13px;
    pointer-events: none;
    text-shadow: 0 0 6px rgba(0,0,0,0.9), 0 0 12px rgba(0,0,0,0.6);
    transform: translate(-50%, -50%);
    white-space: nowrap;
    transition: opacity 0.15s;
    will-change: transform, opacity;
}

.label-3d .katex { font-size: 1em; }
.label-axis { font-size: 18px; font-weight: bold; }

/* Legend */
#legend {
    position: absolute;
    bottom: 16px;
    right: 16px;
    background: rgba(10, 10, 25, 0.85);
    border: 1px solid rgba(100, 100, 255, 0.15);
    border-radius: 8px;
    padding: 10px 14px;
    z-index: 50;
    backdrop-filter: blur(8px);
    max-width: 250px;
}

#legend.hidden { display: none; }

.legend-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    font-size: 0.8em;
    color: #c0c0e0;
}

.legend-swatch {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    flex-shrink: 0;
}

.legend-clickable {
    cursor: pointer;
    user-select: none;
    transition: opacity 0.2s;
}
.legend-clickable:hover { opacity: 0.8; }
.legend-hidden { opacity: 0.4; }
.legend-hidden span { text-decoration: line-through; }

.legend-item .katex { font-size: 0.95em; }

/* Status Bar */
#status-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 22px;
    background: rgba(0, 80, 160, 0.5);
    color: rgba(220, 220, 240, 0.9);
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    padding: 0 10px;
    flex-shrink: 0;
}
#cam-status {
    display: inline-flex;
    align-items: center;
    position: relative;
    cursor: default;
    padding: 1px 5px;
    border-radius: 10px;
    background: rgba(100, 180, 255, 0.10);
    border: 1px solid rgba(100, 180, 255, 0.25);
    user-select: none;
    margin-left: 6px;
}
.cam-status-icon { font-size: 12px; line-height: 1; }
.cam-status-popup {
    display: none;
    position: absolute;
    bottom: 26px;
    left: 0;
    min-width: 240px;
    background: rgba(8, 12, 28, 0.97);
    border: 1px solid rgba(100, 180, 255, 0.35);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 11px;
    line-height: 2;
    color: rgba(220, 230, 255, 0.95);
    box-shadow: 0 6px 24px rgba(0,0,0,0.6);
    z-index: 9999;
    white-space: pre;
    font-family: 'SF Mono', 'Fira Code', monospace;
}
#cam-status:hover .cam-status-popup { display: block; }
#debug-status-text { margin-left: auto; opacity: 0.8; }

/* Slider Status Pill */
#slider-status {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    position: relative;
    cursor: default;
    padding: 1px 6px;
    border-radius: 10px;
    background: rgba(255, 200, 80, 0.15);
    border: 1px solid rgba(255, 200, 80, 0.3);
    user-select: none;
}
#slider-status.hidden { display: none; }
.slider-status-icon { font-size: 11px; line-height: 1; }
.slider-status-count { font-size: 10px; color: rgba(255, 200, 80, 0.9); font-weight: bold; }

/* Tooltip popup */
.slider-status-tooltip {
    display: none;
    position: absolute;
    bottom: 26px;
    left: 0;
    min-width: 160px;
    max-width: 300px;
    background: rgba(10, 14, 30, 0.97);
    border: 1px solid rgba(255, 200, 80, 0.4);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 11px;
    line-height: 1.8;
    color: rgba(220, 220, 240, 0.95);
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
    z-index: 9999;
    white-space: pre;
}
#slider-status:hover .slider-status-tooltip { display: block; }

/* Memory Status Pill */
#memory-status {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    position: relative;
    cursor: pointer;
    padding: 1px 6px;
    border-radius: 10px;
    background: rgba(120, 200, 255, 0.12);
    border: 1px solid rgba(120, 200, 255, 0.3);
    user-select: none;
    margin-left: 6px;
}
#memory-status.hidden { display: none; }
.memory-status-icon { font-size: 11px; line-height: 1; }
.memory-status-count { font-size: 10px; color: rgba(120, 200, 255, 0.9); font-weight: bold; }

/* Memory Popup */
#memory-popup {
    position: fixed;
    bottom: 32px;
    left: 12px;
    width: 420px;
    max-height: 55vh;
    background: rgba(8, 12, 28, 0.97);
    border: 1px solid rgba(120, 200, 255, 0.35);
    border-radius: 8px;
    box-shadow: 0 6px 24px rgba(0,0,0,0.6);
    z-index: 9999;
    display: flex;
    flex-direction: column;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 12px;
}
#memory-popup.hidden { display: none; }
#memory-popup-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 12px;
    border-bottom: 1px solid rgba(120, 200, 255, 0.2);
    color: rgba(120, 200, 255, 0.9);
    font-size: 11px;
    font-weight: bold;
    flex-shrink: 0;
}
#memory-popup-close {
    background: none;
    border: none;
    color: rgba(180, 180, 220, 0.7);
    cursor: pointer;
    font-size: 15px;
    line-height: 1;
    padding: 0 2px;
}
#memory-popup-close:hover { color: #fff; }
#memory-popup-search-wrap {
    padding: 7px 12px;
    border-bottom: 1px solid rgba(120, 200, 255, 0.15);
    flex-shrink: 0;
}
#memory-popup-search {
    width: 100%;
    background: rgba(12, 18, 40, 0.92);
    border: 1px solid rgba(120, 200, 255, 0.28);
    border-radius: 5px;
    color: rgba(205, 225, 255, 0.92);
    padding: 5px 8px;
    font-family: inherit;
    font-size: 11px;
    outline: none;
}
#memory-popup-search::placeholder { color: rgba(150, 175, 220, 0.5); }
#memory-popup-search:focus {
    border-color: rgba(120, 200, 255, 0.55);
    box-shadow: 0 0 0 2px rgba(120, 200, 255, 0.12);
}
#memory-popup-body {
    overflow-y: auto;
    padding: 8px 12px;
    color: rgba(200, 220, 255, 0.9);
    line-height: 1.6;
}
.memory-entry { margin-bottom: 8px; }
.memory-entry-key { color: rgba(120, 200, 255, 0.95); font-weight: bold; }
.memory-entry-summary { color: rgba(160, 200, 160, 0.85); font-size: 11px; margin-left: 4px; }
.memory-entry-preview {
    color: rgba(180, 180, 200, 0.7);
    font-size: 10px;
    margin-top: 2px;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 60px;
    overflow: hidden;
}
#memory-popup-empty { color: rgba(160,160,180,0.5); font-style: italic; padding: 4px 0; }
#memory-popup-no-results { color: rgba(160,160,180,0.5); font-style: italic; padding: 4px 0; }

/* Settings Panel */
#viewport-top-left {
    position: absolute;
    top: 12px;
    left: 12px;
    z-index: 100;
    display: flex;
    align-items: center;
    gap: 6px;
}
#settings-toggle {
    cursor: pointer;
    font-size: 18px;
    color: rgba(180, 180, 220, 0.8);
    user-select: none;
    transition: color 0.2s, background 0.2s;
    background: rgba(10, 10, 25, 0.6);
    border-radius: 6px;
    padding: 4px 7px;
    line-height: 1;
}
#settings-toggle:hover { color: rgba(200, 200, 255, 1); background: rgba(30, 30, 60, 0.8); }
#settings-toggle.active { color: rgba(140, 160, 255, 1); background: rgba(30, 30, 60, 0.8); }

#settings-panel {
    position: absolute;
    top: 48px;
    left: 12px;
    z-index: 100;
    background: rgba(10, 10, 25, 0.9);
    border: 1px solid rgba(100, 100, 255, 0.15);
    border-radius: 8px;
    padding: 10px 12px;
    backdrop-filter: blur(8px);
    min-width: 160px;
}
#settings-panel.hidden { display: none; }

.sp-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 0 4px 0;
    border-bottom: 1px solid rgba(100, 100, 255, 0.1);
    margin-bottom: 4px;
}
.sp-header > span:first-child {
    min-width: 55px;
}
.sp-col-label {
    font-size: 0.65em;
    color: rgba(140, 140, 200, 0.5);
    text-transform: uppercase;
    letter-spacing: 1px;
    text-align: center;
    width: 76px;
}
.sp-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 0;
    font-size: 0.8em;
    color: #c0c0e0;
}
.sp-row > span:first-child {
    min-width: 55px;
}
.sp-ctrl {
    display: flex;
    align-items: center;
    gap: 2px;
}
.sp-val {
    width: 28px;
    text-align: center;
    font-size: 0.85em;
    color: rgba(160, 170, 255, 0.9);
}
.sp-btn {
    width: 22px;
    height: 22px;
    border: 1px solid rgba(100, 100, 255, 0.25);
    border-radius: 4px;
    background: rgba(40, 40, 80, 0.6);
    color: #b0b0e0;
    font-size: 14px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
}
.sp-btn:hover { background: rgba(60, 60, 120, 0.8); color: #fff; }
.sp-btn:active { background: rgba(80, 80, 160, 0.8); }

.sp-divider { height: 1px; background: rgba(100, 100, 200, 0.2); margin: 6px 0 4px; }
.sp-section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: rgba(140,150,220,0.7); margin-bottom: 4px; padding-left: 2px; }
.sp-light-row { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #c0c0e0; margin-bottom: 4px; }
.sp-light-row > span:first-child { min-width: 55px; }
.sp-light-slider { flex: 1; height: 3px; accent-color: #8888ff; cursor: pointer; }
.sp-light-val { min-width: 34px; text-align: right; font-size: 11px; color: rgba(160,170,255,0.8); }
.sp-help { font-size: 11px; color: rgba(188, 198, 238, 0.88); line-height: 1.45; padding: 1px 2px 0; }
.sp-help div { margin-bottom: 2px; }

/* Controls Hint */
#controls-hint {
    display: none;
}

/* Drag-and-drop overlay */
#drop-overlay {
    display: none;
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background: rgba(60, 60, 200, 0.15);
    border: 3px dashed rgba(100, 100, 255, 0.6);
    z-index: 300;
    align-items: center;
    justify-content: center;
    font-size: 1.4em;
    color: rgba(160, 160, 255, 0.9);
}

#drop-overlay.active { display: flex; }

#file-input { display: none; }

/* Scene Dock */
#scene-dock {
    display: none;
    flex-shrink: 0;
}
#scene-dock.visible {
    display: flex;
}
#scene-dock-toggle {
    width: 36px;
    background: rgba(15, 15, 35, 0.95);
    border-right: 1px solid rgba(100, 100, 255, 0.15);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 12px;
    cursor: pointer;
    color: rgba(180, 180, 220, 0.8);
    font-size: 18px;
    user-select: none;
    transition: color 0.2s, background 0.2s;
    flex-shrink: 0;
}
#scene-dock-toggle:hover {
    color: rgba(200, 200, 255, 1);
    background: rgba(25, 25, 50, 0.95);
}
#scene-dock-toggle.active {
    color: rgba(140, 160, 255, 1);
}
#scene-dock-panel {
    width: 0;
    overflow: hidden;
    background: rgba(15, 15, 35, 0.95);
    border-right: 1px solid rgba(100, 100, 255, 0.15);
    display: flex;
    flex-direction: column;
    transition: width 0.2s ease;
    flex-shrink: 0;
}
#scene-dock-panel.open {
    width: 220px;
}
#scene-tree {
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
}
.tree-scene {
    cursor: pointer;
    user-select: none;
}
.tree-scene-header {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 6px 10px;
    font-size: 0.82em;
    font-weight: 600;
    color: #c0c0e0;
    transition: background 0.15s, color 0.15s;
}
.tree-scene-header:hover {
    background: rgba(80, 80, 200, 0.2);
    color: #fff;
}
.tree-scene.active > .tree-scene-header {
    color: #fff;
    background: rgba(80, 80, 200, 0.3);
}
.tree-scene-arrow {
    font-size: 10px;
    width: 14px;
    text-align: center;
    transition: transform 0.15s;
    flex-shrink: 0;
    color: rgba(160, 160, 220, 0.6);
}
.tree-scene.expanded > .tree-scene-header > .tree-scene-arrow {
    transform: rotate(90deg);
}
.tree-steps {
    display: none;
    padding-left: 18px;
}
.tree-scene.expanded > .tree-steps {
    display: block;
}
.tree-step {
    padding: 4px 10px 4px 12px;
    font-size: 0.78em;
    color: #a0a0c0;
    cursor: pointer;
    border-left: 2px solid transparent;
    transition: all 0.15s;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.tree-step:hover {
    background: rgba(80, 80, 200, 0.15);
    color: #d0d0ff;
}
.tree-step.active {
    color: #fff;
    border-left-color: rgba(140, 160, 255, 0.8);
    background: rgba(80, 80, 200, 0.25);
}
.tree-step.visited {
    color: #b0b0d0;
}
.tree-step.visited::before {
    content: '\\2713 ';
    color: rgba(100, 200, 100, 0.6);
    font-size: 0.85em;
}
.tree-scene-header .katex,
.tree-step .katex {
    font-size: 0.9em;
    color: inherit;
}

/* Scene Nav Bar */
#scene-nav {
    position: absolute;
    bottom: 12px;
    left: 16px;
    z-index: 65;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(10, 10, 30, 0.55);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(100, 100, 255, 0.15);
    border-radius: 10px;
    padding: 6px 10px;
}
.nav-btn {
    width: 32px;
    height: 32px;
    border: 1px solid rgba(100, 100, 255, 0.25);
    border-radius: 6px;
    background: rgba(40, 40, 80, 0.6);
    color: #b0b0e0;
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
}
.nav-btn:hover {
    background: rgba(60, 60, 120, 0.8);
    color: #fff;
    border-color: rgba(140, 140, 255, 0.5);
}
.nav-btn:active {
    background: rgba(80, 80, 160, 0.8);
}
.nav-btn.playing {
    color: #44ff88;
    border-color: rgba(80, 200, 120, 0.5);
    background: rgba(40, 80, 60, 0.6);
}

/* Empty state */
#empty-state {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;
    color: rgba(150, 150, 200, 0.6);
    z-index: 30;
    pointer-events: none;
}

/* Top-right action toggles */
#follow-angle-lock-toggle,
#explain-toggle {
    position: absolute;
    top: 12px;
    z-index: 100;
    cursor: pointer;
    font-size: 18px;
    color: rgba(180, 180, 220, 0.8);
    user-select: none;
    transition: color 0.2s;
    background: rgba(10, 10, 25, 0.6);
    border: 1px solid rgba(100, 100, 255, 0.2);
    border-radius: 6px;
    padding: 4px 7px;
    line-height: 1;
    display: none;
}
#explain-toggle { right: 12px; }
#follow-angle-lock-toggle { right: 48px; }
#follow-angle-lock-toggle:hover,
#explain-toggle:hover { color: rgba(200, 200, 255, 1); background: rgba(30, 30, 60, 0.8); }
#follow-angle-lock-toggle.active,
#explain-toggle.active { color: rgba(140, 160, 255, 1); background: rgba(30, 30, 60, 0.8); }
#follow-angle-lock-toggle.cam-active { border-color: rgba(120, 170, 255, 0.35); }

/* Panel Resize Handle */
#panel-resize-handle {
    width: 4px;
    cursor: col-resize;
    background: transparent;
    transition: background 0.15s;
    flex-shrink: 0;
    display: none;
}
#panel-resize-handle:hover,
#panel-resize-handle.dragging { background: rgba(100, 100, 255, 0.3); }

/* Explanation Panel (tabbed) */
#explanation-panel {
    width: 380px;
    min-width: 250px;
    max-width: 600px;
    background: rgba(15, 15, 35, 0.95);
    backdrop-filter: blur(12px);
    border-left: 1px solid rgba(100, 100, 255, 0.15);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
#explanation-panel.hidden { display: none; }

/* Panel Tabs */
#panel-tabs {
    display: flex;
    flex-shrink: 0;
    border-bottom: 1px solid rgba(100, 100, 255, 0.12);
    background: rgba(10, 10, 30, 0.5);
}
.panel-tab {
    flex: 1;
    padding: 8px 0;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: rgba(160, 160, 200, 0.6);
    font-size: 0.82em;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    letter-spacing: 0.5px;
}
.panel-tab:hover { color: rgba(200, 200, 240, 0.9); background: rgba(40, 40, 80, 0.3); }
.panel-tab.active { color: #c0c8ff; border-bottom-color: rgba(140, 160, 255, 0.7); }
.panel-tab.has-chat { position: relative; }

/* Tab Content */
.tab-content { display: none; flex: 1; overflow: hidden; flex-direction: column; }
.tab-content.active { display: flex; }
#tab-doc { overflow-y: auto; }
#tab-chat { display: none; flex-direction: column; }
#tab-chat.active { display: flex; }
#chat-tts-controls {
    display: flex;
    gap: 6px;
    align-items: center;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(100, 100, 255, 0.1);
    background: rgba(10, 10, 28, 0.4);
    flex-shrink: 0;
}
.chat-control-btn {
    padding: 4px 10px;
    background: rgba(30, 30, 65, 0.7);
    border: 1px solid rgba(100, 100, 255, 0.2);
    border-radius: 4px;
    color: rgba(170, 170, 215, 0.9);
    font-size: 0.76em;
    cursor: pointer;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.chat-control-btn:hover {
    background: rgba(50, 50, 100, 0.85);
    color: #e0e0ff;
    border-color: rgba(140, 140, 255, 0.4);
}
.style-picker { position: relative; }
.style-btn { max-width: 185px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.style-palette {
    position: fixed;
    top: 0;
    left: 0;
    width: 320px;
    max-height: 320px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px;
    background: rgba(12, 14, 30, 0.96);
    border: 1px solid rgba(105, 115, 170, 0.55);
    border-radius: 12px;
    box-shadow: 0 18px 36px rgba(0, 0, 0, 0.38);
    z-index: 10050;
}
.style-palette[hidden] { display: none !important; }
.style-backdrop {
    position: fixed;
    inset: 0;
    background: transparent;
    z-index: 10040;
}
.style-backdrop[hidden] { display: none !important; }
.style-search {
    width: 100%;
    box-sizing: border-box;
    background: rgba(24, 28, 50, 0.92);
    border: 1px solid rgba(100, 100, 255, 0.24);
    color: #d8dcff;
    padding: 8px 10px;
    border-radius: 10px;
    font-size: 12px;
}
.style-search:focus { outline: 1px solid rgba(140, 160, 255, 0.72); }
.style-list { overflow-y: auto; padding-right: 4px; }
.style-group { margin-bottom: 8px; }
.style-group-title {
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: rgba(160, 170, 220, 0.65);
    margin: 6px 6px 4px;
}
.style-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 8px;
    font-size: 12px;
    cursor: pointer;
    transition: background 0.12s ease;
}
.style-item:hover { background: rgba(60, 70, 110, 0.45); }
.style-item.active {
    background: rgba(90, 115, 220, 0.28);
    box-shadow: inset 0 0 0 1px rgba(130, 150, 255, 0.4);
}
.style-item-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.style-item-badge {
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 999px;
    border: 1px solid rgba(115, 120, 170, 0.5);
    color: rgba(188, 194, 235, 0.72);
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.style-empty {
    padding: 12px;
    color: rgba(175, 185, 230, 0.64);
    font-size: 12px;
    text-align: center;
}

/* AI Ask Button (inline, per element) */
.ai-ask-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 19px;
    height: 19px;
    margin-left: 5px;
    padding: 0;
    background: rgba(80, 80, 200, 0.15);
    border: 1px solid rgba(120, 120, 255, 0.25);
    border-radius: 50%;
    color: rgba(150, 160, 240, 0.5);
    cursor: pointer;
    vertical-align: middle;
    position: relative;
    top: -1px;
    opacity: 0;
    transition: opacity 0.12s, background 0.12s, color 0.12s, box-shadow 0.12s;
    flex-shrink: 0;
}
#explanation-content h1:hover .ai-ask-btn,
#explanation-content h2:hover .ai-ask-btn,
#explanation-content h3:hover .ai-ask-btn,
#explanation-content p:hover .ai-ask-btn,
#explanation-content li:hover .ai-ask-btn { opacity: 1; }
#scene-description:hover .ai-ask-btn { opacity: 1; }
.ai-ask-btn:hover {
    background: rgba(100, 110, 240, 0.3);
    color: #c8d4ff;
    border-color: rgba(160, 180, 255, 0.6);
    box-shadow: 0 0 6px rgba(120, 140, 255, 0.35);
}
.ai-ask-btn svg { pointer-events: none; }
/* Button after a block element (e.g. display math): right-align on its own line */
.ai-ask-btn.ai-ask-btn--after-block {
    display: block;
    margin-left: auto;
    margin-top: 4px;
}

/* Doc Toolbar (Speak / Commentate) */
#doc-toolbar {
    display: flex;
    gap: 6px;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(100, 100, 255, 0.1);
    flex-shrink: 0;
    background: rgba(10, 10, 28, 0.4);
}
#doc-toolbar button {
    padding: 4px 10px;
    background: rgba(30, 30, 65, 0.7);
    border: 1px solid rgba(100, 100, 255, 0.2);
    border-radius: 4px;
    color: rgba(170, 170, 215, 0.85);
    font-size: 0.76em;
    cursor: pointer;
    transition: background 0.15s, color 0.15s, border-color 0.15s;
    white-space: nowrap;
}
#doc-toolbar button:hover:not(:disabled) {
    background: rgba(50, 50, 100, 0.85);
    color: #e0e0ff;
    border-color: rgba(140, 140, 255, 0.4);
}
#doc-toolbar button.active {
    background: rgba(70, 70, 150, 0.85);
    border-color: rgba(140, 160, 255, 0.6);
    color: #c8d0ff;
}
#doc-toolbar button:disabled {
    opacity: 0.45;
    cursor: default;
}

#explanation-content {
    padding: 24px 20px;
}

#explanation-content h1 {
    font-size: 20px;
    font-weight: 600;
    color: #ffffff;
    margin: 0 0 12px 0;
    line-height: 1.3;
}
#explanation-content h2 {
    font-size: 17px;
    font-weight: 600;
    color: #b0c0ff;
    margin: 20px 0 8px 0;
    line-height: 1.3;
}
#explanation-content h3 {
    font-size: 15px;
    font-weight: 600;
    color: #c0b0ff;
    margin: 16px 0 6px 0;
    line-height: 1.3;
}
#explanation-content p {
    font-size: 14px;
    line-height: 1.7;
    color: #d0d0d0;
    margin: 0 0 12px 0;
}
#explanation-content ul, #explanation-content ol {
    font-size: 14px;
    line-height: 1.7;
    color: #d0d0d0;
    margin: 0 0 12px 0;
    padding-left: 20px;
}
#explanation-content li { margin-bottom: 4px; }
#explanation-content strong { color: #e0e0ff; }
#explanation-content em { color: #c8c8e0; }
#explanation-content code {
    background: rgba(40, 40, 80, 0.6);
    padding: 2px 5px;
    border-radius: 3px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.88em;
    color: #c0c0ff;
}
#explanation-content pre {
    background: rgba(10, 10, 30, 0.8);
    border: 1px solid rgba(100, 100, 255, 0.1);
    border-radius: 6px;
    padding: 12px;
    margin: 0 0 12px 0;
    overflow-x: auto;
}
#explanation-content pre code {
    background: none;
    padding: 0;
    font-size: 13px;
}
#explanation-content blockquote {
    border-left: 3px solid rgba(100, 120, 255, 0.4);
    padding: 8px 14px;
    margin: 0 0 12px 0;
    color: #b0b0d0;
    font-style: italic;
}
#explanation-content hr {
    border: none;
    border-top: 1px solid rgba(100, 100, 255, 0.15);
    margin: 16px 0;
}
#explanation-content .katex { color: #e0d0ff; font-size: 0.92em; }
#explanation-content .katex-display { margin: 12px 0; overflow: hidden; }

/* Step Caption Overlay */
#step-caption {
    position: absolute;
    bottom: 64px;
    left: 50%;
    transform: translateX(-50%);
    max-width: 70%;
    background: rgba(10, 10, 30, 0.55);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(100, 100, 255, 0.15);
    border-radius: 10px;
    padding: 10px 46px 10px 18px;
    color: rgba(224, 224, 240, 0.85);
    font-size: 0.9em;
    line-height: 1.5;
    z-index: 60;
    pointer-events: auto;
    cursor: grab;
    transition: opacity 0.3s ease;
    text-align: center;
}
#step-caption:active { cursor: grabbing; }
#step-caption.hidden { opacity: 0; }
#step-caption .katex { color: rgba(224, 208, 255, 0.9); font-size: 0.95em; }
.caption-ai-btn {
    position: absolute;
    top: 50%;
    right: 12px;
    transform: translateY(-50%);
    margin-left: 0;
}
#step-caption:hover .caption-ai-btn { opacity: 1; }

/* Slider Overlay */
#slider-overlay {
    position: absolute;
    bottom: 56px;
    left: 16px;
    z-index: 65;
    background: rgba(10, 10, 30, 0.55);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(100, 100, 255, 0.15);
    border-radius: 10px;
    padding: 10px 14px;
    min-width: 220px;
    max-width: 340px;
}
#slider-overlay.hidden { display: none; }
.slider-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
}
.slider-row + .slider-row { margin-top: 4px; }
.slider-label {
    min-width: 40px;
    font-size: 0.82em;
    color: rgba(224, 208, 255, 0.9);
    white-space: nowrap;
}
.slider-label .katex { font-size: 0.92em; }
.slider-range {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 4px;
    background: rgba(100, 100, 200, 0.3);
    border-radius: 2px;
    outline: none;
    cursor: pointer;
    min-width: 80px;
}
.slider-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 14px;
    height: 14px;
    background: rgba(140, 160, 255, 0.9);
    border-radius: 50%;
    cursor: pointer;
    border: none;
    transition: background 0.15s;
}
.slider-range::-webkit-slider-thumb:hover { background: rgba(170, 180, 255, 1); }
.slider-range::-moz-range-thumb {
    width: 14px;
    height: 14px;
    background: rgba(140, 160, 255, 0.9);
    border-radius: 50%;
    cursor: pointer;
    border: none;
}
.slider-value {
    min-width: 32px;
    text-align: right;
    font-size: 0.78em;
    color: rgba(160, 170, 255, 0.9);
    font-family: 'SF Mono', 'Fira Code', monospace;
}
.slider-play-btn {
    background: rgba(80, 100, 220, 0.2);
    border: 1px solid rgba(120, 140, 255, 0.35);
    border-radius: 50%;
    width: 22px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    color: rgba(160, 180, 255, 0.9);
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.15s, border-color 0.15s;
}
.slider-play-btn:hover {
    background: rgba(80, 100, 220, 0.4);
    border-color: rgba(160, 180, 255, 0.7);
}
.slider-drag-handle {
    text-align: center;
    cursor: grab;
    color: rgba(140, 140, 200, 0.35);
    font-size: 13px;
    letter-spacing: 3px;
    padding: 2px 0 5px;
    margin-bottom: 4px;
    border-bottom: 1px solid rgba(100, 100, 200, 0.12);
    user-select: none;
}
.slider-drag-handle:hover { color: rgba(160, 160, 220, 0.7); }
#slider-overlay.dragging { opacity: 0.85; cursor: grabbing !important; }

/* Info Overlays */
#info-overlays { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 55; }
.info-overlay {
    position: absolute;
    display: flex;
    align-items: flex-start;
    gap: 8px;
    background: rgba(8, 8, 28, 0.65);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(100, 110, 255, 0.2);
    border-radius: 10px;
    padding: 7px 36px 7px 8px;
    color: rgba(224, 224, 240, 0.92);
    font-size: 0.88em;
    line-height: 1.6;
    width: max-content;
    max-width: 480px;
    pointer-events: all;
    cursor: grab;
    user-select: none;
    transition: border-color 0.15s, background 0.15s;
}
.info-overlay:hover { border-color: rgba(130, 150, 255, 0.5); background: rgba(14, 14, 40, 0.8); }
.info-overlay.dragging { cursor: grabbing; opacity: 0.85; }
.info-overlay.collapsed { padding: 5px 6px; }
.info-overlay.collapsed .info-overlay-content { display: none; }
.info-overlay-toggle {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border: none;
    background: transparent;
    color: rgba(140, 170, 255, 0.7);
    font-size: 14px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    padding: 0;
    transition: color 0.15s;
    margin-top: 1px;
}
.info-overlay-toggle:hover { color: rgba(200, 220, 255, 1); }
.info-overlay-ai-btn {
    position: absolute;
    top: 50%;
    right: 8px;
    transform: translateY(-50%);
    width: 20px;
    height: 20px;
    border: 1px solid rgba(120, 120, 255, 0.25);
    background: rgba(80, 80, 200, 0.12);
    border-radius: 50%;
    color: rgba(140, 160, 240, 0.55);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    opacity: 0;
    transition: color 0.15s, background 0.15s, border-color 0.15s, box-shadow 0.15s, opacity 0.12s;
}
.info-overlay:hover .info-overlay-ai-btn { opacity: 1; }
.info-overlay-ai-btn:hover {
    background: rgba(100, 110, 240, 0.3);
    color: #c8d4ff;
    border-color: rgba(160, 180, 255, 0.6);
    box-shadow: 0 0 6px rgba(120, 140, 255, 0.35);
}
.info-overlay-ai-btn svg { pointer-events: none; }
.info-overlay.collapsed .info-overlay-ai-btn { display: none; }
.info-overlay-content { flex: 1; }
/* Default positions — top offsets account for viewport buttons */
.info-overlay.pos-top-left     { top: 58px; left: 14px; }
.info-overlay.pos-top-right    { top: 86px; right: 14px; }
.info-overlay.pos-bottom-left  { bottom: 80px; left: 14px; }
.info-overlay.pos-bottom-right { bottom: 80px; right: 14px; }
.info-overlay.pos-top-center   { top: 58px; left: 50%; transform: translateX(-50%); }
.info-overlay .katex { color: rgba(210, 220, 255, 0.95); }
.info-overlay .katex-display { margin: 4px 0; overflow: visible; }

#empty-state h2 {
    font-size: 1.6em;
    margin-bottom: 8px;
    font-weight: 300;
}

#empty-state p { font-size: 0.9em; }

/* Chat unavailable notice */
#chat-unavailable-msg.hidden { display: none; }
#chat-unavailable-msg {
    margin: 24px 16px 8px;
    padding: 14px 16px;
    background: rgba(255, 180, 50, 0.08);
    border: 1px solid rgba(255, 180, 50, 0.25);
    border-radius: 8px;
    color: rgba(230, 200, 140, 0.9);
    font-size: 0.85em;
    line-height: 1.6;
}
#chat-unavailable-msg p { margin: 0 0 6px; }
#chat-unavailable-msg p:last-child { margin-bottom: 0; }
#chat-unavailable-msg code {
    background: rgba(255,255,255,0.08);
    border-radius: 3px;
    padding: 1px 5px;
    font-family: monospace;
    color: rgba(180, 220, 255, 0.9);
}
#tab-chat.unavailable #chat-input-area { display: none; }
#tab-chat.unavailable #chat-tts-controls { display: none; }

/* Chat Messages */
#chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

/* Chat Messages */
.chat-msg {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    animation: chatFadeIn 0.2s ease;
}
@keyframes chatFadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
}
.msg-avatar {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    border-radius: 50%;
    background: rgba(40, 40, 80, 0.5);
}
.chat-msg.user .msg-avatar { background: rgba(60, 60, 120, 0.5); }
.msg-body {
    flex: 1;
    font-size: 13px;
    line-height: 1.6;
    color: #d0d0e0;
    min-width: 0;
    overflow-wrap: break-word;
}
.chat-msg.user .msg-body {
    background: rgba(60, 60, 140, 0.2);
    border-radius: 10px 10px 2px 10px;
    padding: 8px 12px;
    color: #e0e0f0;
}
.chat-msg.assistant .msg-body {
    padding: 2px 0;
}
/* Markdown inside chat messages */
.msg-body p { margin: 0 0 8px 0; }
.msg-body p:last-child { margin-bottom: 0; }
.msg-body ul, .msg-body ol { margin: 4px 0 8px 0; padding-left: 18px; }
.msg-body li { margin-bottom: 2px; }
.msg-body code {
    background: rgba(40, 40, 80, 0.6);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 0.9em;
    color: #c0c0ff;
}
.msg-body strong { color: #e0e0ff; }
.msg-body .katex { color: #e0d0ff; font-size: 0.92em; }

/* Per-message speak button */
.msg-speak-btn {
    flex-shrink: 0;
    align-self: flex-start;
    margin-top: 3px;
    width: 22px;
    height: 22px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    position: relative;
    background: none;
    border: 1px solid transparent;
    border-radius: 50%;
    color: rgba(150, 160, 180, 0.55);
    cursor: pointer;
    transition: color 0.12s, background 0.12s, border-color 0.12s;
}
.chat-msg:hover .msg-speak-btn {
    color: rgba(170, 185, 255, 0.8);
    border-color: rgba(100, 100, 255, 0.2);
}
.msg-speak-btn.idle {
    color: rgba(150, 160, 180, 0.55);
    background: rgba(35, 40, 55, 0.18);
    border-color: rgba(95, 105, 135, 0.28);
}
.msg-speak-btn:hover {
    color: #c0c8ff !important;
    background: rgba(80, 80, 200, 0.2);
    border-color: rgba(120, 120, 255, 0.35);
}
.msg-speak-btn.active {
    color: #a0d0ff !important;
    background: rgba(60, 100, 220, 0.25);
    border-color: rgba(100, 160, 255, 0.5);
    opacity: 1 !important;
    animation: speak-pulse 1.2s ease-in-out infinite;
}
.msg-speak-btn.active::after {
    content: '';
    position: absolute;
    inset: -3px;
    border-radius: 999px;
    border: 1px solid rgba(120, 180, 255, 0.45);
    animation: speak-ring 1.2s ease-out infinite;
    pointer-events: none;
}
.msg-speak-btn.paused {
    color: rgba(170, 178, 196, 0.9) !important;
    background: rgba(45, 52, 72, 0.34);
    border-color: rgba(110, 122, 156, 0.45);
    opacity: 1 !important;
}
@keyframes speak-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(100, 160, 255, 0.28); transform: scale(1); }
    50%      { box-shadow: 0 0 0 5px rgba(100, 160, 255, 0.0); transform: scale(1.06); }
}
@keyframes speak-ring {
    0%   { opacity: 0.75; transform: scale(0.94); }
    100% { opacity: 0.0; transform: scale(1.22); }
}
.msg-speak-btn.loading {
    color: #b7c7ee !important;
    background: linear-gradient(90deg, rgba(40, 50, 90, 0.25), rgba(75, 95, 170, 0.35), rgba(40, 50, 90, 0.25));
    background-size: 180% 100%;
    border-color: rgba(110, 140, 220, 0.5);
    opacity: 1 !important;
    font-size: 11px;
    letter-spacing: 1.5px;
    line-height: 1;
    animation: tts-loading 0.9s ease-in-out infinite, tts-shade 1.1s linear infinite;
}
@keyframes tts-loading {
    from { opacity: 0.3; }
    to   { opacity: 1;   }
}
@keyframes tts-shade {
    from { background-position: 0% 0%; }
    to   { background-position: 180% 0%; }
}
.msg-speak-btn svg { pointer-events: none; }

.chat-msg.tts-speaking .msg-body {
    border-color: rgba(105, 165, 255, 0.55);
    box-shadow: 0 0 0 1px rgba(90, 150, 255, 0.22), 0 0 14px rgba(70, 120, 220, 0.18);
}
.chat-msg.tts-loading .msg-body {
    border-color: rgba(120, 145, 220, 0.4);
}
.chat-msg.tts-paused .msg-body {
    border-color: rgba(95, 180, 135, 0.45);
}
.chat-msg.tts-speaking .msg-speak-btn,
.chat-msg.tts-loading .msg-speak-btn,
.chat-msg.tts-paused .msg-speak-btn {
    color: #c6d8ff;
    border-color: rgba(120, 140, 230, 0.45);
}

/* Loading dots */
.chat-loading {
    display: flex;
    gap: 4px;
    padding: 8px 4px !important;
}
.chat-loading span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: rgba(140, 160, 255, 0.5);
    animation: chatDot 1.2s infinite ease-in-out;
}
.chat-loading span:nth-child(2) { animation-delay: 0.2s; }
.chat-loading span:nth-child(3) { animation-delay: 0.4s; }
@keyframes chatDot {
    0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
    40% { transform: scale(1); opacity: 1; }
}

/* Tool Call Chips */
.chat-tool-call {
    margin-left: 36px;
    padding: 4px 10px;
    background: rgba(40, 60, 100, 0.2);
    border: 1px solid rgba(100, 140, 200, 0.15);
    border-radius: 6px;
    font-size: 0.78em;
    color: rgba(160, 180, 220, 0.8);
    cursor: pointer;
    transition: background 0.15s;
}
.chat-tool-call:hover { background: rgba(40, 60, 100, 0.35); }
.tool-call-summary { line-height: 1.5; }
.tool-call-details {
    margin-top: 4px;
    padding: 6px 8px;
    background: rgba(10, 10, 30, 0.5);
    border-radius: 4px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.92em;
    white-space: pre-wrap;
    color: rgba(150, 160, 200, 0.7);
}
.tool-call-details.hidden { display: none; }
/* Preset Prompt Chips */
#preset-prompts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 6px 10px 4px;
    border-top: 1px solid rgba(255,255,255,0.06);
}
#preset-prompts.hidden { display: none; }

.preset-prompt-btn {
    background: rgba(80, 130, 220, 0.12);
    border: 1px solid rgba(100, 160, 255, 0.28);
    border-radius: 14px;
    padding: 4px 12px;
    font-size: 12px;
    color: rgba(160, 200, 255, 0.9);
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    white-space: nowrap;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: inherit;
}
.preset-prompt-btn:hover {
    background: rgba(80, 130, 220, 0.25);
    border-color: rgba(100, 160, 255, 0.55);
    color: rgba(200, 225, 255, 1);
}
/* Chat Input Area */
#chat-input-area {
    display: flex;
    gap: 8px;
    padding: 10px 12px;
    border-top: 1px solid rgba(100, 100, 255, 0.12);
    background: rgba(10, 10, 30, 0.4);
    flex-shrink: 0;
    align-items: flex-end;
}
#chat-input {
    flex: 1;
    background: rgba(30, 30, 60, 0.6);
    border: 1px solid rgba(100, 100, 255, 0.2);
    border-radius: 8px;
    padding: 8px 12px;
    color: #e0e0f0;
    font-size: 13px;
    font-family: inherit;
    resize: none;
    outline: none;
    max-height: 120px;
    line-height: 1.5;
    transition: border-color 0.2s;
}
#chat-input:focus { border-color: rgba(140, 160, 255, 0.5); }
#chat-input::placeholder { color: rgba(140, 140, 180, 0.5); }
#chat-send {
    background: rgba(80, 80, 200, 0.3);
    border: 1px solid rgba(100, 100, 255, 0.3);
    border-radius: 8px;
    color: #b0b8ff;
    width: 36px;
    height: 36px;
    cursor: pointer;
    font-size: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
    flex-shrink: 0;
}
#chat-send:hover { background: rgba(80, 80, 200, 0.5); color: #fff; }
'''


NAVIGATE_TOOL_DECL = types.FunctionDeclaration(
    name="navigate_to",
    description="Navigate to a specific scene and step. When the user says 'next', advance by ONE step within the CURRENT scene (current step + 1). Only change the scene number when the user asks for a different topic. Always check Current State in the system prompt for your current scene and step before navigating.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "scene": types.Schema(type="INTEGER", description="Scene number (1-based). Scene 1 is the first scene, scene 2 is the second, etc."),
            "step": types.Schema(type="INTEGER", description="Step number within the scene. 0 = root/base scene (before any steps). 1 = first step, 2 = second step, etc."),
            "reason": types.Schema(type="STRING", description="Brief user-facing explanation of why navigating here"),
        },
        required=["scene", "step"],
    ),
)

SET_CAMERA_TOOL_DECL = types.FunctionDeclaration(
    name="set_camera",
    description="Set the 3D camera angle. Either use a named preset view OR specify a custom position. The available view names are listed in the current state context.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "view": types.Schema(
                type="STRING",
                description="Name of a preset camera view (e.g. 'iso', 'top', 'right'). Use this instead of position/target when a preset fits.",
            ),
            "position": types.Schema(
                type="ARRAY",
                items=types.Schema(type="NUMBER"),
                description="Custom camera position as [x, y, z] in world coordinates. Ignored if view is set.",
            ),
            "target": types.Schema(
                type="ARRAY",
                items=types.Schema(type="NUMBER"),
                description="Point the camera looks at as [x, y, z] (defaults to [0,0,0]). Only used with position.",
            ),
            "zoom": types.Schema(
                type="NUMBER",
                description="Zoom multiplier. Values > 1 zoom in (closer), < 1 zoom out (farther). Only used with position.",
            ),
            "reason": types.Schema(type="STRING", description="Brief explanation of why this angle is useful"),
        },
        required=[],
    ),
)

ADD_SCENE_TOOL_DECL = types.FunctionDeclaration(
    name="add_scene",
    description="Add a new 3D scene to the lesson. Pass scene fields (title, elements, steps, etc.) as DIRECT top-level arguments — do NOT nest them under a 'scene' key. The scene is appended and the client auto-navigates to it — do NOT call navigate_to after add_scene. See system prompt for the full scene schema and examples.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "title": types.Schema(type="STRING", description="Scene title (supports LaTeX)"),
            "description": types.Schema(type="STRING", description="Caption below viewport"),
            "markdown": types.Schema(type="STRING", description="Explanation panel with LaTeX"),
            "elements": types.Schema(type="ARRAY", items=types.Schema(type="OBJECT"), description="Array of element objects. Element labels support LaTeX — wrap math in $...$, e.g. \"$\\\\vec{a}_c$\" not \"\\\\vec{a}_c\". Plain text labels (no math) don't need $...$."  ),
            "steps": types.Schema(type="ARRAY", items=types.Schema(type="OBJECT"), description="Progressive reveal steps"),
            "range": types.Schema(type="ARRAY", items=types.Schema(type="ARRAY", items=types.Schema(type="NUMBER")), description="Axis ranges [[xmin,xmax],[ymin,ymax],[zmin,zmax]]"),
            "camera": types.Schema(type="OBJECT", description="Camera position and target", properties={
                "position": types.Schema(type="ARRAY", items=types.Schema(type="NUMBER")),
                "target": types.Schema(type="ARRAY", items=types.Schema(type="NUMBER")),
            }),
        },
        required=["title", "elements"],
    ),
)


SET_SLIDERS_TOOL_DECL = types.FunctionDeclaration(
    name="set_sliders",
    description="Animate one or more sliders to target values. Use this to demonstrate how changing parameters affects the visualization. Each slider animates smoothly over ~800ms. Only use when sliders are active (listed in Current State).",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "values": types.Schema(
                type="OBJECT",
                description="Map of slider ID to target numeric value, e.g. {\"ax\": 4, \"bx\": 2}. Slider IDs are listed in the Active sliders section of Current State.",
            ),
        },
        required=["values"],
    ),
)


from gemini_live_tools import safe_eval_math, eval_math_sweep, MATH_NAMES, HAS_NUMPY


def _build_eval_math_description():
    """Build tool description dynamically from the actual MATH_NAMES registry."""
    # Constants: names whose values are plain numbers
    constants = sorted(k for k, v in MATH_NAMES.items() if isinstance(v, (int, float)))
    # Functions: everything else
    functions = sorted(k for k in MATH_NAMES if k not in constants)

    desc = (
        "Evaluate a math expression and return the exact numeric result. "
        "Use this to compute magnitudes, dot products, angles, areas, or any formula — "
        "so you can cite precise numbers rather than approximating. "
        "Current slider values are automatically available as variables by their ID.\n\n"
        "IMPORTANT: Use PYTHON syntax, not JavaScript. "
        "sin(x) not Math.sin(x), sqrt(x) not Math.sqrt(x), x**2 not x^2.\n\n"
        f"Available functions: {', '.join(functions)}.\n"
        f"Available constants: {', '.join(constants)}.\n\n"
        "VECTOR/MATRIX usage: pass vectors/matrices as variables (lists or nested lists), "
        "e.g. variables={a: [1,2,3], M: [[1,2],[3,4]]}. "
        "Use vec([x,y,z]) to construct a vector inline. "
        "Use A @ b for matrix-vector multiply."
    )
    if not HAS_NUMPY:
        desc += " (Note: numpy not available — vector/matrix functions disabled.)"
    return desc


EVAL_MATH_TOOL_DECL = types.FunctionDeclaration(
    name="eval_math",
    description=_build_eval_math_description(),
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "expression": types.Schema(
                type="STRING",
                description=(
                    "PYTHON syntax (not JavaScript): use sin(x) not Math.sin(x), "
                    "sqrt(x) not Math.sqrt(x), x**2 not x^2 or Math.pow(x,2). "
                    "Scalar examples: 'sqrt(ax**2 + ay**2 + az**2)', 'degrees(atan2(ay, ax))'. "
                    "Vector examples (pass vectors via variables): "
                    "'norm(a)', 'dot(a, b)', 'angle(a, b)', 'normalize(a)', "
                    "'proj(a, b)', 'A @ b', 'det(M)'. "
                    "Inline vector literal: 'norm(vec([3,4,0]))'."
                ),
            ),
            "variables": types.Schema(
                type="OBJECT",
                description=(
                    "Optional name→value bindings. Scalars (numbers) and vectors/matrices "
                    "(lists or nested lists) are both accepted. "
                    "Slider IDs are injected automatically as scalars. "
                    "Example: {a: [1,2,3], b: [4,5,6], M: [[1,2],[3,4]]}. "
                    "Keys are plain identifiers — do NOT quote them."
                ),
            ),
            "sweep_var": types.Schema(
                type="STRING",
                description="Variable to sweep over, e.g. 'x' or 'theta'. Required when using sweep_start/sweep_end or sweep_values.",
            ),
            "sweep_start": types.Schema(type="NUMBER", description="Sweep range start value."),
            "sweep_end": types.Schema(type="NUMBER", description="Sweep range end value."),
            "sweep_steps": types.Schema(type="INTEGER", description="Number of points in the sweep (default 64)."),
            "sweep_values": types.Schema(
                type="ARRAY",
                items=types.Schema(type="NUMBER"),
                description="Explicit list of values to sweep over (alternative to sweep_start/sweep_end/sweep_steps).",
            ),
            "store_as": types.Schema(
                type="STRING",
                description=(
                    "Store the result in agent memory under this key instead of returning the full value. "
                    "Use short descriptive names like 'sin_pts', 'froms', 'eigenvals'. "
                    "Stored values are automatically available as variables in future eval_math expressions. "
                    "Reference them in add_scene element fields as '$key' (e.g. 'points': '$sin_pts'). "
                    "Always use store_as for large arrays (sweep results, matrices) to keep context small."
                ),
            ),
        },
        required=["expression"],
    ),
)


MEM_GET_TOOL_DECL = types.FunctionDeclaration(
    name="mem_get",
    description=(
        "Retrieve a value from agent memory by key. "
        "Returns the stored value (scalar, list, or nested list). "
        "Use to inspect what's stored, verify a value, or pass it to another context. "
        "Call mem_get('?') to list all stored keys."
    ),
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "key": types.Schema(
                type="STRING",
                description="Memory key to retrieve. Pass '?' to list all stored keys and their shapes.",
            ),
        },
        required=["key"],
    ),
)

MEM_SET_TOOL_DECL = types.FunctionDeclaration(
    name="mem_set",
    description=(
        "Store any value in agent memory under a key. "
        "Use for scalars, vectors, matrices, or any data you want to reference later. "
        "Stored values can be referenced as variables in eval_math, or as '$key' in add_scene fields. "
        "Prefer eval_math store_as for computed results; use mem_set for literals you already have."
    ),
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "key": types.Schema(
                type="STRING",
                description="Key to store under. Short descriptive name, e.g. 'origin', 'basis_x', 'pts'.",
            ),
            "value": types.Schema(
                type="OBJECT",
                description="Value to store. Can be a number, list, nested list, or any JSON-serializable data.",
            ),
        },
        required=["key", "value"],
    ),
)


SET_PRESET_PROMPTS_TOOL_DECL = types.FunctionDeclaration(
    name="set_preset_prompts",
    description="Set suggested prompt chips above the chat input. Users see these as clickable buttons — click sends immediately, shift-click fills the input for editing. Pass an empty list to clear all chips. Use proactively to suggest relevant follow-ups after explaining a concept or completing a task. Call at most once per turn — calling it twice replaces the first set.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "prompts": types.Schema(
                type="ARRAY",
                items=types.Schema(type="STRING"),
                description="Prompt strings to display as chips. 2-5 suggestions recommended. Keep each under 60 characters.",
            ),
        },
        required=["prompts"],
    ),
)


SET_INFO_OVERLAY_TOOL_DECL = types.FunctionDeclaration(
    name="set_info_overlay",
    description="Add, update, or remove a floating info overlay on the 3D canvas. Overlays render LaTeX and live math that updates automatically when sliders change. Use {slider_id} placeholders for live values. Call with clear=true to remove all overlays. Use proactively to show matrix representations, formulas, or key values while users explore a scene.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "id": types.Schema(
                type="STRING",
                description="Unique identifier for this overlay (e.g. 'matrix', 'formula'). Reuse the same id to update an existing overlay.",
            ),
            "content": types.Schema(
                type="STRING",
                description="Content to display — same rendering as step captions: $...$ inline math, $$...$$ display math, plain text, \\n for line breaks. Use {slider_id} for live slider values, e.g. '$$\\\\begin{pmatrix} {a} & {b} \\\\\\\\ {c} & {d} \\\\end{pmatrix}$$'. CRITICAL: placeholders must be plain {id} — NEVER \\{id\\}. Backslash-escaping breaks the placeholder so it shows literally instead of the value. Omit when clear=true.",
            ),
            "position": types.Schema(
                type="STRING",
                description="Position on canvas: 'top-left' (default), 'top-right', 'top-center', 'bottom-left', or 'bottom-right'.",
            ),
            "clear": types.Schema(
                type="BOOLEAN",
                description="If true, remove all overlays and ignore id/content.",
            ),
        },
    ),
)

ALL_TOOL_DECLS = [
    NAVIGATE_TOOL_DECL,
    SET_CAMERA_TOOL_DECL,
    ADD_SCENE_TOOL_DECL,
    SET_SLIDERS_TOOL_DECL,
    EVAL_MATH_TOOL_DECL,
    MEM_GET_TOOL_DECL,
    MEM_SET_TOOL_DECL,
    SET_PRESET_PROMPTS_TOOL_DECL,
    SET_INFO_OVERLAY_TOOL_DECL,
]

def _make_tools(*exclude_names):
    decls = [d for d in ALL_TOOL_DECLS if d.name not in exclude_names]
    return [types.Tool(function_declarations=decls)]


def _load_agent_tools_reference():
    """Load the agent tools reference from the external markdown file."""
    ref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent-tools-reference.md')
    try:
        with open(ref_path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        return ""

_AGENT_TOOLS_REFERENCE = _load_agent_tools_reference()


def build_system_prompt(context):
    """Build system prompt with full visualization context."""
    parts = ["You are an AI math tutor embedded in an interactive 3D linear algebra visualization.\n"]

    # Current scene definition — the full JSON the client is rendering
    scene = context.get('currentScene', {})
    runtime = context.get('runtime', {})

    parts.append("## Current State\n*This entire system prompt is rebuilt fresh on every user message. Always trust it over chat history for scene, step, camera, sliders, and all UI state. If chat history contradicts STATE, ignore history and follow STATE.*")
    if context.get('lessonTitle'):
        parts.append(f"- Lesson: {context['lessonTitle']}")
    scene_num = context.get('sceneNumber', 1)  # 1-based
    total_scenes = context.get('totalScenes', '?')
    if scene.get('title'):
        parts.append(f"- Scene: {scene_num}/{total_scenes} \"{scene['title']}\"")
    step_num = runtime.get('stepNumber', 0)  # 0=root, 1=first step, etc.
    total_steps = len(scene.get('steps', []))
    if step_num == 0:
        parts.append(f"- Step: 0 (root/base scene) — {total_steps} steps available" if total_steps > 0 else "- Step: 0 (root/base scene, no steps)")
    elif total_steps > 0 and step_num >= 1 and step_num <= total_steps:
        step = scene['steps'][step_num - 1]
        parts.append(f"- Step: {step_num}/{total_steps}: \"{step.get('title', '')}\"")
    if runtime.get('cameraPosition'):
        cp = runtime['cameraPosition']
        parts.append(f"- Camera position: ({cp['x']}, {cp['y']}, {cp['z']})")
    if runtime.get('cameraTarget'):
        ct = runtime['cameraTarget']
        parts.append(f"- Camera target: ({ct['x']}, {ct['y']}, {ct['z']})")
    if runtime.get('cameraViews'):
        parts.append(f"- Available camera views: {', '.join(runtime['cameraViews'])}")
    if runtime.get('visibleElements'):
        items = [f"{e.get('label', e.get('type', '?'))} ({e.get('type', '?')})" for e in runtime['visibleElements']]
        parts.append(f"- Visible elements: {', '.join(items)}")
    if runtime.get('sliders'):
        slider_parts = []
        for k, v in runtime['sliders'].items():
            if isinstance(v, dict):
                label = v.get('label', k)
                slider_parts.append(f"{label} ({k})={v['value']} [range: {v['min']}..{v['max']}, step: {v['step']}]")
            else:
                slider_parts.append(f"{k}={v}")
        parts.append(f"- Active sliders: {', '.join(slider_parts)}")
    if runtime.get('currentCaption'):
        parts.append(f"- Caption displayed to user: \"{runtime['currentCaption']}\"")
    if runtime.get('projection'):
        parts.append(f"- Projection: {runtime['projection']}")
    if runtime.get('activeTab'):
        parts.append(f"- User viewing: {runtime['activeTab']} panel")

    # Scene tree for navigation
    if context.get('sceneTree'):
        parts.append("\n## Lesson Structure")
        parts.append("All numbers are 1-based. Use `navigate_to(scene=N, step=0)` for root, `step=1` for first step.")
        for s in context['sceneTree']:
            parts.append(f"- Scene {s['sceneNumber']}: {s['title']}")
            if s.get('steps'):
                for st in s['steps']:
                    desc = f" — {st['description']}" if st.get('description') else ''
                    parts.append(f"  - Step {st['stepNumber']}: {st['title']}{desc}")

    # Scene JSON — only include steps up to the current one so the agent
    # doesn't read ahead and explain future steps.
    if scene:
        step_num = runtime.get('stepNumber', 0)
        scene_for_prompt = {k: v for k, v in scene.items() if k not in ('markdown', 'prompt', 'steps')}
        if scene.get('steps'):
            # Only include steps up to and including the current step (1-based → slice [:step_num])
            scene_for_prompt['steps'] = scene['steps'][:step_num] if step_num > 0 else []
        parts.append(f"\n## Current Scene Definition\n```json\n{json.dumps(scene_for_prompt, indent=2)}\n```")

    # Current explanation (from scene markdown)
    if scene.get('markdown'):
        parts.append(f"\n## Current Explanation\n{scene['markdown']}")

    # Agent memory — show stored keys so agent knows what's available
    if _agent_memory:
        mem_lines = [f"  - {k}: {_memory_summary(k, v)}" for k, v in _agent_memory.items()]
        parts.append("\n## Agent Memory\nKeys currently in memory (reference as variable or '$key' in add_scene):\n" + "\n".join(mem_lines))

    # Scene-specific agent instructions
    if scene.get('prompt'):
        parts.append(f"\n## Scene Instructions\n{scene['prompt']}")

    # Instructions
    parts.append("""
## Instructions
- You are a math tutor. **Only call `add_scene` when the user explicitly asks to "show", "visualize", "build", "create", or "plot" something, or when you are already in an active scene and a new visualization clearly improves understanding.** For questions, explanations, calculations, and navigation — answer in chat without building a new scene. Unsolicited scene creation distracts the user and risks errors.
- Use LaTeX ($...$) for math notation — the client renders it. Always use single backslashes: `$\\theta$` not `$\\\\theta$`.
- **Navigation**: When the user says "next", go to the NEXT STEP within the CURRENT scene: keep the same scene number, increment step by 1. Only change the scene number when the user explicitly asks for a different topic. Scenes are 1-based (scene 1 = first). Steps: 0 = root/base, 1 = first step, 2 = second, etc. Always read your current position from the Current State section above before navigating.
- **One navigation per turn**: Never call `navigate_to` more than once in a single response. If reaching a destination requires multiple hops, navigate one step at a time and wait for the user to reply before continuing. Do not auto-advance through multiple steps in one turn.
- Use the set_camera tool to adjust the viewing angle. You can use a preset view name (e.g. view="top") or custom position/target coordinates. Use zoom (e.g. zoom=1.5 closer, zoom=0.5 farther) with custom positions to control distance.
- Use the tools in whatever order makes sense for the request. You have full discretion.
- Build scenes with 4-7 steps that progressively reveal the concept. Each step should have a detailed, conversational description that teaches.
- Always include a comprehensive markdown explanation with LaTeX formulas, definitions, and worked calculations.
- Describe what's visible when asked "what am I looking at?"
- **Keep chat responses short and direct** — 1–3 sentences max for explanations, unless the user explicitly asks for more detail. Let the scene, steps, and markdown panel do the heavy teaching. Never re-explain what's already visible in the scene or markdown.
- **Unclear or vague questions**: If the user's request is ambiguous (e.g. "show me something", "explain it", "make it nicer"), do NOT guess — ask one clarifying question. Keep the question brief: "Which part would you like explained?" or "Do you mean [X] or [Y]?"
- Do not write scene JSON as text in chat — make tool calls so things actually render.
- **CRITICAL**: Always call `set_preset_prompts` as a function tool call. NEVER write the prompts as JSON text in your response.
- **CRITICAL**: NEVER use `{expr}` placeholders in your chat response text. Placeholders like `{theta}` or `{v.toFixed(1)}` only work inside `set_info_overlay` content, not in chat messages. In chat, write computed values directly or describe them in words.
- **STATE over history**: The Current State section is always authoritative for scene, step, sliders, and camera.
- **Tool capabilities**:
  - `eval_math`: compute exact numbers. When asked to "compute", "calculate", "get", or "make a series" — call `eval_math` and let the result appear in chat. To sweep a range: set `sweep_var="x"`, `sweep_start`, `sweep_end`, `sweep_steps`. Only pipe the result into `add_scene` if the user also wants a visualization. Expression syntax is Python: `sin(x)` not `Math.sin(x)`, `x**2` not `x^2`.
  - `add_scene`: build a visualization. **Only call when the user explicitly requests it or when it clearly serves the current interaction — not as a default response to every question.** A `line` with many `points` draws a curve; `vectors` with `froms`/`tos` arrays draws a series of arrows. Do not hardcode arrays that could be computed — use `eval_math` first.
  - `set_sliders`: animate sliders to show how parameters change the visualization.
  - `set_preset_prompts`: call this **once** per response to surface 2–4 follow-up chips. Always a function call — never inline JSON. Never call it more than once per turn.
  - `set_info_overlay`: show a live LaTeX panel on the canvas. Use `{slider_id}` placeholders so values update automatically as the user moves sliders — write `{a}` not `\\{a\\}` (backslash-escaping breaks the placeholder). Always add a matrix overlay when sliders define a matrix. Call with `clear: true` to remove all overlays.
  - `parametric_curve`: continuous smooth curve from a JavaScript expression — use this only when a slider drives the shape live and exact point values are not needed.
""")

    # Agent tools reference (loaded from external file)
    if _AGENT_TOOLS_REFERENCE:
        parts.append(_AGENT_TOOLS_REFERENCE)

    # Log context breadcrumbs — which sections were included
    sections = ["Current State", "Lesson Structure", "Current Scene Definition", "Current Explanation", "Scene Instructions", "Instructions", "Agent Tools Reference"]
    included = []
    prompt_text = "\n".join(parts)
    for s in sections:
        if f"## {s}" in prompt_text:
            included.append(s)
    print(f"   📋 System prompt sections: {', '.join(included)} ({len(prompt_text)} chars)")

    return prompt_text


# Lazy-initialized Gemini client
_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def _detect_navigation(message, context):
    """Detect simple navigation commands and return (scene, step, direction) or None."""
    msg = message.strip().lower()
    nav_next = msg in ('next', 'next step', 'continue', 'go on', 'forward', 'n')
    nav_prev = msg in ('previous', 'prev', 'back', 'go back', 'previous step', 'p')
    if not nav_next and not nav_prev:
        return None

    scene_num = context.get('sceneNumber', 1)
    runtime = context.get('runtime', {})
    current_step = runtime.get('stepNumber', 0)  # 0=root, 1=first step
    scene = context.get('currentScene', {})
    total_steps = len(scene.get('steps', []))

    if nav_next:
        target_step = current_step + 1
        if target_step > total_steps:
            # At last step — try next scene
            total_scenes = context.get('totalScenes', 1)
            if scene_num < total_scenes:
                return (scene_num + 1, 0, 'next_scene')
            return None  # nowhere to go
        return (scene_num, target_step, 'next')
    else:  # nav_prev
        target_step = current_step - 1
        if target_step < 0:
            # At root — try previous scene
            if scene_num > 1:
                return (scene_num - 1, 0, 'prev_scene')
            return None
        return (scene_num, target_step, 'prev')


def _extract_inline_preset_prompts(text, tool_calls):
    """Detect {"prompts": [...]} JSON embedded in text by Gemini instead of a tool call.
    Strips it from the text and appends a synthetic set_preset_prompts tool call entry."""
    import re
    match = re.search(r'\{[^{}]*"prompts"\s*:\s*\[[^\]]*\][^{}]*\}', text, re.DOTALL)
    if not match:
        return text
    try:
        obj = json.loads(match.group(0))
        prompts = obj.get('prompts')
        if not isinstance(prompts, list):
            return text
    except (json.JSONDecodeError, ValueError):
        return text
    print(f"   ⚠️  Gemini wrote set_preset_prompts as inline JSON — recovering: {prompts}")
    tool_calls.append({
        "name": "set_preset_prompts",
        "rawArgs": {"prompts": prompts},
        "args": {"prompts": prompts},
        "result": {"status": "success", "count": len(prompts),
                   "message": f"Set {len(prompts)} preset prompt{'s' if len(prompts) != 1 else ''}."},
    })
    cleaned = (text[:match.start()] + text[match.end():]).strip()
    return cleaned


def call_gemini_chat(message, history, context):
    """Call Gemini API using google-genai SDK. Returns (response_text, tool_calls_list, debug_info)."""
    client = get_gemini_client()
    if not client:
        return "AI chat is not available (no API key configured).", [], {}

    # Handle simple navigation deterministically — don't rely on the agent
    nav = _detect_navigation(message, context)
    if nav:
        scene_num, step_num, direction = nav
        current_scene = context.get('currentScene', {})

        # For same-scene navigation, use current scene data.
        # For cross-scene, look up the target scene from the scene tree.
        scene_tree = context.get('sceneTree', [])
        if direction in ('next_scene', 'prev_scene'):
            # Get target scene info from scene tree
            target_idx = scene_num - 1
            if 0 <= target_idx < len(scene_tree):
                tree_entry = scene_tree[target_idx]
                step_title = tree_entry.get('title', '')
                step_desc = ''
                steps = tree_entry.get('steps', [])
            else:
                step_title = ''
                step_desc = ''
                steps = []
        else:
            steps = current_scene.get('steps', [])
            if step_num == 0:
                step_title = current_scene.get('title', '')
                step_desc = current_scene.get('description', '')
            elif 1 <= step_num <= len(steps):
                s = steps[step_num - 1]
                step_title = s.get('title', '')
                step_desc = s.get('description', '')
            else:
                step_title = ''
                step_desc = ''

        tc_result = {"status": "success", "navigated": True,
                     "scene": scene_num, "step": step_num}
        tool_calls = [{"name": "navigate_to", "args": {"scene": scene_num, "step": step_num}, "result": tc_result}]

        # Update context to reflect the navigation target so the system prompt is current
        context['sceneNumber'] = scene_num
        if 'runtime' not in context:
            context['runtime'] = {}
        context['runtime']['stepNumber'] = step_num

        # Rewrite navigation command into an explanation request.
        # The agent sees the updated Current State and knows what step it's on.
        explain_prompt = "What am I looking at now?"

        # Build system prompt with updated context
        system_prompt = build_system_prompt(context)
        contents = []
        for msg in (history or []):
            role = 'user' if msg.get('role') == 'user' else 'model'
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.get('text', ''))]))
        contents.append(types.Content(role='user', parts=[types.Part.from_text(text=explain_prompt)]))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=_make_tools('navigate_to'),
            temperature=0.7,
        )
        print(f"   ⏩ Auto-navigation: scene {scene_num}, step {step_num} ({direction})")

        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=config,
            )
            text = ""
            if response.candidates and response.candidates[0].content.parts:
                text = "".join(p.text for p in response.candidates[0].content.parts if p.text)
            debug_info = {"systemPrompt": system_prompt, "contents": [c.to_json_dict() for c in contents]}
            return text.strip() or "Let me walk you through this step.", tool_calls, debug_info
        except Exception as e:
            return f"Navigated to step {step_num}.", tool_calls, {}

    system_prompt = build_system_prompt(context)

    # Build contents list
    contents = []
    for msg in (history or []):
        role = 'user' if msg.get('role') == 'user' else 'model'
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg.get('text', ''))]))

    # Add current user message
    contents.append(types.Content(role='user', parts=[types.Part.from_text(text=message)]))

    # Log history summary
    for i, msg in enumerate(history or []):
        preview = (msg.get('text', '') or '')[:80].replace('\n', ' ')
        print(f"   💬 history[{i}] {msg.get('role','?')}: {preview}")
    print(f"   💬 current: {message[:80]}")

    tool_calls = []
    added_scenes_count = 0
    max_turns = 10

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=_make_tools(),
        temperature=0.7,
    )

    # Build debug payload — the full picture of what Gemini sees
    debug_info = {
        "systemPrompt": system_prompt,
        "contents": [c.to_json_dict() for c in contents],
    }

    # Log request summary
    tool_names = [d.name for d in config.tools[0].function_declarations] if config.tools else []
    print(f"   🤖 Gemini request: model={GEMINI_MODEL}, {len(contents)} messages, tools=[{', '.join(tool_names)}], system_prompt={len(system_prompt)} chars")

    if DEBUG_MODE:
        print(f"\n🤖 GEMINI REQUEST: {json.dumps({'model': GEMINI_MODEL, **debug_info})}\n")

    for turn in range(max_turns):
        if turn > 0:
            print(f"   🔄 Gemini turn {turn + 1}/{max_turns} ({len(contents)} messages)")
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=config,
            )
        except Exception as e:
            return f"Gemini API error: {str(e)}", [], debug_info

        # Log finish reason for debugging
        finish = None
        if response.candidates:
            candidate = response.candidates[0]
            finish = getattr(candidate, 'finish_reason', None)
            if finish:
                print(f"   Gemini finish_reason: {finish}")
            if str(finish) not in ('MAX_TOKENS', 'STOP', 'FinishReason.MAX_TOKENS', 'FinishReason.STOP'):
                print(f"   ⚠️  Unexpected finish_reason: {finish}")

        if not response.candidates:
            return "", tool_calls, debug_info

        parts = response.candidates[0].content.parts
        if not parts:
            print(f"   ⚠️  Empty parts (Gemini returned STOP with no content) — nudging to respond")
            contents.append(types.Content(role='model', parts=[types.Part.from_text(text="(no response)")]))
            contents.append(types.Content(role='user', parts=[types.Part.from_text(
                text="Please respond to my request.")]))
            continue

        # Log all response parts for debugging
        for i, p in enumerate(parts):
            if p.text:
                print(f"   📝 part[{i}].text: {p.text}")
            if p.function_call:
                fc = p.function_call
                print(f"   🔧 part[{i}].function_call: {fc.name}({json.dumps(dict(fc.args) if fc.args else {}, default=str)[:300]})")
            if not p.text and not p.function_call:
                print(f"   ❓ part[{i}] unknown: {str(p)[:300]}")

        # Handle malformed function call — retry
        if str(finish) in ('MALFORMED_FUNCTION_CALL', 'FinishReason.MALFORMED_FUNCTION_CALL'):
            print(f"   ❌ Malformed function call — asking Gemini to retry")
            contents.append(types.Content(role='model', parts=parts))
            contents.append(types.Content(role='user', parts=[types.Part.from_text(
                text="Your previous function call was malformed. Please respond with plain text instead, or retry the tool call with valid JSON arguments.")]))
            continue

        # Check for function calls vs text. Gemini may return multiple function calls
        # in a single turn; execute all of them in order.
        function_calls = []
        text_response = ""
        for part in parts:
            if part.function_call:
                function_calls.append(part.function_call)
            if part.text:
                text_response += part.text

        if function_calls:
            # Preserve the model response (including thought_signature) once.
            contents.append(types.Content(role='model', parts=parts))
            must_continue = False

            for fc in function_calls:
                tc_name = fc.name

                # Convert args to plain Python dict (handle proto objects)
                tc_args = {}
                if fc.args:
                    # Try multiple conversion strategies for proto Struct/MapComposite
                    raw_args = fc.args
                    if hasattr(raw_args, 'model_dump'):
                        tc_args = raw_args.model_dump()
                    elif hasattr(raw_args, 'to_json_dict'):
                        tc_args = raw_args.to_json_dict()
                    elif isinstance(raw_args, dict):
                        tc_args = dict(raw_args)
                    else:
                        tc_args = dict(raw_args)

                    # Deep-convert any remaining proto objects to plain Python types
                    def _to_plain(obj):
                        if isinstance(obj, (str, int, float, bool, type(None))):
                            return obj
                        if isinstance(obj, dict):
                            return {k: _to_plain(v) for k, v in obj.items()}
                        if isinstance(obj, (list, tuple)):
                            return [_to_plain(v) for v in obj]
                        # Proto MapComposite, RepeatedComposite, etc.
                        if hasattr(obj, 'items'):
                            return {k: _to_plain(v) for k, v in obj.items()}
                        if hasattr(obj, '__iter__'):
                            return [_to_plain(v) for v in obj]
                        return str(obj)

                    tc_args = _to_plain(tc_args)
                raw_tc_args = json.loads(json.dumps(tc_args, default=str))

                # Log the full tool call JSON
                print(f"\n🔧 TOOL CALL: {tc_name}")
                try:
                    print(json.dumps(tc_args, indent=2, ensure_ascii=True, default=str))
                except Exception as log_err:
                    print(f"   (could not serialize args: {log_err})")
                    print(f"   args keys: {list(tc_args.keys())}")

                # For add_scene: the scene properties are now top-level args (not nested under "scene")
                if tc_name == 'add_scene':
                    # Resolve $key memory references in element fields before building the scene
                    tc_args = _resolve_memory_refs(tc_args)
                    # Unwrap if agent nested the scene under a "scene" key (common hallucination)
                    if isinstance(tc_args.get('scene'), dict) and 'title' in tc_args.get('scene', {}):
                        print(f"   ⚠️  add_scene: agent wrapped scene under 'scene' key — unwrapping")
                        tc_args = {**tc_args['scene']}
                    # Build scene object from top-level args
                    scene_obj = {k: v for k, v in tc_args.items() if k not in ('_parseError',)}
                    tc_args['parsedScene'] = scene_obj
                    print(f"   ✅ scene object — {len(scene_obj.get('elements', []))} elements, "
                          f"{len(scene_obj.get('steps', []))} steps, title: {scene_obj.get('title', '?')}")
                # Track add_scene calls so navigate_to validation accounts for newly added scenes
                if tc_name == 'add_scene':
                    added_scenes_count = added_scenes_count + 1

                # Build tool result with context
                scene_count = len(context.get('sceneTree', [])) + added_scenes_count
                if tc_name == 'add_scene':
                    new_scene_num = scene_count  # 1-based number of the newly added scene
                    tc_result = {"status": "success", "newSceneNumber": new_scene_num,
                                 "message": f"Scene added as scene {new_scene_num}. The client will auto-navigate to it. Do NOT call navigate_to."}
                elif tc_name == 'navigate_to':
                    # Agent sends 1-based scene numbers
                    req_scene = int(tc_args.get('scene', 0))  # 1-based
                    req_step = int(tc_args.get('step', 0))
                    # Validate scene (1-based: valid range is 1 to scene_count)
                    if req_scene < 1 or req_scene > scene_count:
                        tc_result = {"status": "error",
                                     "error": f"Scene {req_scene} out of range. Valid: 1-{scene_count}. Check Lesson Structure in system prompt."}
                        print(f"   ❌ navigate_to: scene {req_scene} out of bounds (1-{scene_count})")
                    elif req_step < 0:
                        tc_result = {"status": "error",
                                     "error": f"Step {req_step} invalid. Use 0 for root, 1 for first step, etc."}
                        print(f"   ❌ navigate_to: step {req_step} is negative")
                    else:
                        # Get step count for validation
                        scene_tree = context.get('sceneTree', [])
                        scene_idx_0 = req_scene - 1  # convert to 0-based for lookup
                        target_scene_steps = 0
                        if 0 <= scene_idx_0 < len(scene_tree):
                            target_scene_steps = len(scene_tree[scene_idx_0].get('steps', []))
                        if target_scene_steps > 0 and req_step > target_scene_steps:
                            tc_result = {"status": "error",
                                         "error": f"Step {req_step} out of range for scene {req_scene}. Has {target_scene_steps} steps. Valid: 0 (root) to {target_scene_steps}."}
                            print(f"   ❌ navigate_to: step {req_step} > max {target_scene_steps} for scene {req_scene}")
                        else:
                            tc_result = {"status": "success", "navigated": True,
                                         "scene": req_scene, "step": req_step}
                            # Include target step content so the agent can explain it
                            scene_data = context.get('currentScene', {})
                            if req_scene == context.get('sceneNumber'):
                                # Same scene — use currentScene directly
                                target_scene = scene_data
                            else:
                                # Different scene — look up from scene tree (limited info)
                                target_scene = {}
                            steps = target_scene.get('steps', [])
                            if req_step == 0:
                                tc_result["stepDescription"] = target_scene.get('description', '')
                                tc_result["stepTitle"] = target_scene.get('title', '')
                            elif 1 <= req_step <= len(steps):
                                step = steps[req_step - 1]
                                tc_result["stepDescription"] = step.get('description', '')
                                tc_result["stepTitle"] = step.get('title', '')
                elif tc_name == 'set_sliders':
                    values = tc_args.get('values', {})
                    available = context.get('runtime', {}).get('sliders', {})
                    results = {}
                    for sid, target in values.items():
                        if sid not in available:
                            results[sid] = {"status": "error", "error": f"Unknown slider '{sid}'"}
                        else:
                            s = available[sid]
                            clamped = max(s['min'], min(s['max'], float(target)))
                            results[sid] = {"status": "ok", "from": s['value'], "to": clamped}
                    tc_result = {"status": "success", "sliders": results}
                elif tc_name == 'eval_math':
                    expr = tc_args.get('expression', '')
                    raw_vars = tc_args.get('variables') or {}
                    # Strip spurious surrounding quotes from variable names (agent sometimes double-quotes keys)
                    variables = {k.strip("\"'"): v for k, v in raw_vars.items()}
                    # Convert new flat sweep shape {var, start, end, steps} or {var, values}
                    # into the internal format {var_name: spec} expected by eval_math_sweep
                    sweep_raw = tc_args.get('sweep') or None
                    sweep_var = tc_args.get('sweep_var') or None
                    if sweep_var:
                        if tc_args.get('sweep_values'):
                            sweep = {sweep_var: tc_args['sweep_values']}
                        elif 'sweep_start' in tc_args and 'sweep_end' in tc_args:
                            sweep = {sweep_var: {
                                'start': tc_args['sweep_start'],
                                'end':   tc_args['sweep_end'],
                                'steps': tc_args.get('sweep_steps', 64),
                            }}
                        else:
                            sweep = None
                    else:
                        sweep = None
                    store_as = tc_args.get('store_as') or None
                    # Auto-inject slider values and all agent memory keys as variables
                    for sid, s in context.get('runtime', {}).get('sliders', {}).items():
                        if sid not in variables:
                            variables[sid] = s['value']
                    for mem_key, mem_val in _agent_memory.items():
                        if mem_key not in variables:
                            variables[mem_key] = mem_val
                    if sweep:
                        result, error = eval_math_sweep(expr, variables, sweep)
                    else:
                        result, error = safe_eval_math(expr, variables)
                    if error:
                        tc_result = {"status": "error", "expression": expr, "error": error,
                                     "hint": "Fix the expression and call eval_math again, or call add_scene if you have enough data."}
                        print(f"   ❌ eval_math: {error}")
                    elif store_as:
                        _agent_memory[store_as] = result
                        summary = _memory_summary(store_as, result)
                        tc_result = {"status": "success", "stored_as": store_as, "summary": summary,
                                     "hint": f"Stored. Reference as variable '{store_as}' in eval_math, or as '${store_as}' in add_scene fields."}
                        print(f"   ✅ eval_math → memory['{store_as}']: {summary}")
                    else:
                        n = f"{len(result)}-point sweep" if isinstance(result, list) and sweep else result
                        tc_result = {"status": "success", "expression": expr, "result": result,
                                     "hint": "Tip: use store_as to save large arrays to memory instead of returning inline."}
                        print(f"   ✅ eval_math: {expr} = {n}")
                elif tc_name == 'mem_get':
                    key = tc_args.get('key', '')
                    if key == '?':
                        listing = {k: _memory_summary(k, v) for k, v in _agent_memory.items()}
                        tc_result = {"status": "success", "keys": listing if listing else "(empty)"}
                        print(f"   🗂️  mem_get(?): {list(_agent_memory.keys())}")
                    elif key in _agent_memory:
                        val = _agent_memory[key]
                        tc_result = {"status": "success", "key": key, "value": val,
                                     "summary": _memory_summary(key, val)}
                        print(f"   🗂️  mem_get('{key}'): {_memory_summary(key, val)}")
                    else:
                        tc_result = {"status": "error", "key": key,
                                     "error": f"Key '{key}' not found.",
                                     "available_keys": list(_agent_memory.keys())}
                        print(f"   ❌ mem_get('{key}'): not found")
                elif tc_name == 'mem_set':
                    key = tc_args.get('key', '')
                    value = tc_args.get('value')
                    if not key:
                        tc_result = {"status": "error", "error": "key is required"}
                    else:
                        _agent_memory[key] = value
                        summary = _memory_summary(key, value)
                        tc_result = {"status": "success", "stored_as": key, "summary": summary,
                                     "hint": f"Stored. Reference as variable '{key}' in eval_math, or as '${key}' in add_scene fields."}
                        print(f"   💾 mem_set['{key}']: {summary}")
                elif tc_name == 'set_preset_prompts':
                    prompts = tc_args.get('prompts', [])
                    tc_result = {
                        "status": "success",
                        "count": len(prompts),
                        "message": f"{'Set' if prompts else 'Cleared'} {len(prompts)} preset prompt{'s' if len(prompts) != 1 else ''}.",
                    }
                    print(f"   💬 set_preset_prompts: {prompts}")
                elif tc_name == 'set_info_overlay':
                    if tc_args.get('clear'):
                        tc_result = {"status": "success", "message": "Cleared all info overlays."}
                        print(f"   🖼️  set_info_overlay: cleared all")
                    else:
                        overlay_id = tc_args.get('id', '')
                        content = tc_args.get('content', '')
                        position = tc_args.get('position', 'top-left')
                        tc_result = {
                            "status": "success",
                            "id": overlay_id,
                            "position": position,
                            "message": f"Overlay '{overlay_id}' set at {position}.",
                        }
                        print(f"   🖼️  set_info_overlay['{overlay_id}'] @ {position}: {content[:60]}{'…' if len(content) > 60 else ''}")
                else:
                    tc_result = {"status": "success"}
                tool_calls.append({
                    "name": tc_name,
                    "rawArgs": raw_tc_args,
                    "args": tc_args,
                    "result": tc_result,
                })

                # navigate_to: update context, rebuild system prompt, and strip navigate_to
                # from tools so the agent explains instead of double-navigating.
                if tc_name == 'navigate_to' and tc_result.get('status') == 'success':
                    # Update context to reflect new position (same as deterministic path)
                    req_scene = int(tc_args.get('scene', 0))
                    req_step = int(tc_args.get('step', 0))
                    context['sceneNumber'] = req_scene
                    if 'runtime' not in context:
                        context['runtime'] = {}
                    context['runtime']['stepNumber'] = req_step
                    # Rebuild system prompt with updated state
                    updated_prompt = build_system_prompt(context)

                    tc_result["message"] = "Navigation done. Now explain what the user is seeing."
                    config = types.GenerateContentConfig(
                        system_instruction=updated_prompt,
                        tools=_make_tools('navigate_to'),
                        temperature=config.temperature,
                    )
                    must_continue = True

                # set_sliders: update context with new values, rebuild prompt, strip tool.
                if tc_name == 'set_sliders' and tc_result.get('status') == 'success':
                    if 'runtime' not in context:
                        context['runtime'] = {}
                    if 'sliders' not in context['runtime']:
                        context['runtime']['sliders'] = {}
                    for sid, res in tc_result.get('sliders', {}).items():
                        if res.get('status') == 'ok' and sid in context['runtime']['sliders']:
                            context['runtime']['sliders'][sid]['value'] = res['to']
                    updated_prompt = build_system_prompt(context)
                    tc_result["message"] = "Sliders animated. Now explain what changed in the visualization."
                    remaining_decls = [d for d in config.tools[0].function_declarations if d.name != 'set_sliders']
                    config = types.GenerateContentConfig(
                        system_instruction=updated_prompt,
                        tools=[types.Tool(function_declarations=remaining_decls)] if remaining_decls else [],
                        temperature=config.temperature,
                    )
                    must_continue = True

                # eval_math with store_as requires another model turn so the agent can
                # use the stored value in subsequent calls or explanation.
                if tc_name == 'eval_math' and tc_args.get('store_as'):
                    must_continue = True

                # Feed each tool response back to Gemini in call order.
                contents.append(types.Content(role='user', parts=[
                    types.Part.from_function_response(name=tc_name, response=tc_result)
                ]))

            if text_response.strip() and not must_continue:
                text_response = _extract_inline_preset_prompts(text_response, tool_calls)
                return text_response, tool_calls, debug_info
            continue
        else:
            text_response = _extract_inline_preset_prompts(text_response, tool_calls)
            return text_response or "I'm not sure how to respond to that.", tool_calls, debug_info

    text_response = _extract_inline_preset_prompts(text_response, tool_calls)
    return text_response, tool_calls, debug_info


DEBUG_MODE = False

def serve_and_open(initial_scene_path=None, port=DEFAULT_PORT, json_output=False, debug=False):
    """Serve the MathBoxAI viewer and optionally open in browser."""
    global DEBUG_MODE
    DEBUG_MODE = debug

    html_content = generate_html(debug=debug)
    current_spec = [None]

    # Read app.js once at startup
    with open(app_js_path, 'r') as f:
        app_js_content = f.read()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == '/' or path == '/index.html':
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(html_content.encode('utf-8'))

            elif path == '/app.js':
                # Re-read from disk each time for live development
                with open(app_js_path, 'r') as f:
                    js = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(js.encode('utf-8'))

            elif path == '/chat.js':
                with open(chat_js_path, 'r') as f:
                    js = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(js.encode('utf-8'))

            elif path == '/shared/voice-character-selector.js':
                shared_js_path = script_dir / "static" / "voice-character-selector.js"
                with open(shared_js_path, 'r') as f:
                    js = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(js.encode('utf-8'))

            elif path == '/style.css':
                self.send_response(200)
                self.send_header('Content-Type', 'text/css')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.end_headers()
                self.wfile.write(CSS_CONTENT.encode('utf-8'))

            elif path == '/favicon.ico':
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Cache-Control', 'public, max-age=86400')
                self.end_headers()
                self.wfile.write(FAVICON_SVG.encode('utf-8'))

            elif path == '/api/chat/available':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"available": bool(GEMINI_API_KEY)}).encode('utf-8'))

            elif path == '/api/health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))

            elif path == '/api/memory':
                # Return current agent memory state with per-key summaries
                payload = {
                    k: {"summary": _memory_summary(k, v), "value": v}
                    for k, v in _agent_memory.items()
                }
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode('utf-8'))

            elif path == '/api/scenes':
                scenes = list_builtin_scenes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"scenes": scenes}).encode('utf-8'))

            elif path == '/api/scene_file':
                requested = query.get('path', [''])[0]
                resolved_path = resolve_scene_path(requested)
                if not resolved_path:
                    self.send_response(404)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Scene file not found"}).encode('utf-8'))
                    return
                try:
                    with open(resolved_path, 'r') as f:
                        scene = json.load(f)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "spec": scene,
                        "path": str(resolved_path),
                        "label": resolved_path.name
                    }).encode('utf-8'))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid JSON in scene file"}).encode('utf-8'))
                except Exception as e:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

            elif path.startswith('/scenes/'):
                name = path[8:]
                scene = load_builtin_scene(name)
                if scene:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(scene).encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'Scene not found')

            elif path == '/api/scene':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                data = current_spec[0] if current_spec[0] else {}
                self.wfile.write(json.dumps(data).encode('utf-8'))

            elif path == '/shutdown':
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'Shutting down...')
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0))).start()

            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            from urllib.parse import urlparse
            parsed = urlparse(self.path)
            path = parsed.path

            if path == '/api/chat':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    req = json.loads(body)
                    message = req.get('message', '')
                    history = req.get('history', [])
                    context = req.get('context', {})

                    if not message.strip():
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Empty message"}).encode('utf-8'))
                        return

                    response_text, tool_calls, debug_info = call_gemini_chat(message, history, context)
                    print(f"   💬 Response ({len(response_text)} chars): {response_text}")

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "response": response_text,
                        "toolCalls": tool_calls,
                        "debug": debug_info
                    }).encode('utf-8'))

                except json.JSONDecodeError:
                    print("   ❌ /api/chat: invalid JSON in request body")
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode('utf-8'))
                except Exception as e:
                    import traceback
                    print(f"   ❌ /api/chat error: {e}\n{traceback.format_exc()}")
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

            elif path == '/api/tts':
                if not TTS_AVAILABLE or not GEMINI_API_KEY:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "TTS not available"}).encode('utf-8'))
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    req = json.loads(body)
                    text = req.get('text', '').strip()
                    if not text:
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "Empty text"}).encode('utf-8'))
                        return

                    import time as _time
                    character = req.get('character', 'joker')
                    voice = req.get('voice', 'Charon')
                    mode = req.get('mode', 'read')
                    api = GeminiLiveAPI(api_key=GEMINI_API_KEY, client=get_gemini_client())
                    print(f"\n🔊 TTS: character={character}, voice={voice}, mode={mode}, {len(text)} chars")
                    print(f"🔊 TTS input: {text}")
                    t0 = _time.monotonic()
                    if mode == 'perform':
                        tts_text = api.prepare_text(text, character_name=character)
                        print(f"🔊 TTS prepared ({_time.monotonic()-t0:.2f}s): {tts_text}")
                    else:
                        tts_text = text
                    pcm_chunks = []
                    ok = api.stream_tts(
                        text=tts_text,
                        on_chunk=lambda pcm: pcm_chunks.append(pcm),
                        voice_name=voice,
                        character_name=character,
                        pre_cleaned=(mode == 'perform'),
                    )
                    t1 = _time.monotonic()
                    if ok and pcm_chunks:
                        wav_bytes = pcm_to_wav_bytes(b"".join(pcm_chunks))
                        print(f"🔊 TTS done: total={t1-t0:.2f}s, wav={len(wav_bytes)//1024}KB")
                        self.send_response(200)
                        self.send_header('Content-Type', 'audio/wav')
                        self.send_header('Content-Length', str(len(wav_bytes)))
                        self.end_headers()
                        self.wfile.write(wav_bytes)
                    else:
                        self.send_response(500)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "TTS synthesis failed"}).encode('utf-8'))
                except Exception as e:
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

            elif path == '/api/load':
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                try:
                    new_spec = json.loads(body)
                    current_spec[0] = new_spec
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "loaded"}).encode('utf-8'))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

        def finish(self):
            try:
                super().finish()
            except (BrokenPipeError, ConnectionResetError):
                pass

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd_server = None

    def start_server():
        nonlocal httpd_server
        httpd_server = ThreadedTCPServer(("", port), Handler)
        if not json_output:
            print(f"Server started at http://localhost:{port}")
        httpd_server.serve_forever()

    server_thread = threading.Thread(target=start_server, daemon=False)
    server_thread.start()
    time.sleep(0.5)

    url = f"http://localhost:{port}/"
    if initial_scene_path:
        url = f"{url}?scene={quote(str(initial_scene_path))}"

    if json_output:
        result = {
            "status": "success",
            "url": url,
            "port": port,
            "pid": os.getpid()
        }
        print(json.dumps(result, indent=2))
        sys.stdout.flush()
    else:
        webbrowser.open(url)
        print(f"Opened MathBoxAI in browser")
        print(f"\nDrag & drop JSON files onto the viewport to load scenes")
        print(f"\nPress 'q' or Ctrl+C to stop the server")

    if not json_output:
        if sys.stdin.isatty():
            old_settings = termios.tcgetattr(sys.stdin)
            try:
                tty.setcbreak(sys.stdin.fileno())
                while True:
                    if sys.stdin in select.select([sys.stdin], [], [], 0.1)[0]:
                        char = sys.stdin.read(1)
                        if char.lower() == 'q':
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                            print(f"\nServer stopped")
                            if httpd_server:
                                httpd_server.shutdown()
                            sys.exit(0)
                    time.sleep(0.1)
            except KeyboardInterrupt:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                print(f"\n\nServer stopped")
                if httpd_server:
                    httpd_server.shutdown()
                sys.exit(0)
            finally:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass
        else:
            def signal_handler(signum, frame):
                if httpd_server:
                    httpd_server.shutdown()
                sys.exit(0)
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
            try:
                while True:
                    time.sleep(1)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(
        description='MathBoxAI - Interactive 3D Linear Algebra Visualizer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  mathboxAI                              Launch empty viewer
  mathboxAI scene.json                   Launch with scene
  mathboxAI scenes/vector-addition.json  Load built-in scene
  mathboxAI --port 9000                  Use custom port
        '''
    )
    parser.add_argument('scene', nargs='?', help='Path to scene JSON file')
    parser.add_argument('--json', action='store_true', help='Output JSON (for MCP integration)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'Port (default: {DEFAULT_PORT})')
    parser.add_argument('--debug', action='store_true', help='Dump full Gemini API requests to console')

    args = parser.parse_args()

    if not args.json:
        print(f"Checking port {args.port}...")
    kill_server_on_port(args.port)
    time.sleep(0.5)

    initial_scene_path = None
    if args.scene:
        scene_path = resolve_scene_path(args.scene)
        if not scene_path:
            print(f"Error: Scene file not found: {args.scene}", file=sys.stderr)
            sys.exit(1)
        if not args.json:
            print(f"Loading scene: {scene_path}")
        initial_scene_path = str(scene_path)

    serve_and_open(initial_scene_path, port=args.port, json_output=args.json, debug=args.debug)


if __name__ == "__main__":
    main()
