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

index_html_path = script_dir / "index.html"
style_css_path  = script_dir / "style.css"

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
        candidates = [Path.cwd() / candidate, script_dir / candidate, scenes_dir / candidate]
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
    """Read index.html and inject the debug flag."""
    debug_mode_js = "true" if debug else "false"
    with open(index_html_path, 'r') as f:
        return f.read().replace('__DEBUG_MODE__', debug_mode_js)

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
                with open(style_css_path, 'r') as f:
                    self.wfile.write(f.read().encode('utf-8'))

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
