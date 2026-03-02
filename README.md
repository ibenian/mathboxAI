# mathboxAI

Interactive 3D math visualizer with AI chat and narrated lessons — powered by Gemini.

```
mathboxAI eigenvalues.json
```

![mathboxAI screenshot placeholder]

---

## Quick Start

**Prerequisites:** Python 3.10+, a [Gemini API key](https://aistudio.google.com/apikey)

```bash
git clone https://github.com/ibenian/mathboxAI
cd mathboxAI
pip install -r requirements.txt
export GEMINI_API_KEY=your_key_here
./mathboxAI
```

Open [http://localhost:8785](http://localhost:8785) in your browser.

To launch directly into a scene:

```bash
./mathboxAI scenes/eigenvalues.json
```

---

## Contributing

**Most contributions don't require touching any Python or JavaScript.**
The two most impactful things you can contribute are new scenes and new voice characters.

### Adding a Scene

Scenes are plain JSON files in `scenes/`. Each one is a self-contained interactive lesson.

**Minimal scene:**

```json
{
  "title": "My Scene",
  "description": "A short description shown below the title.",
  "markdown": "## My Scene\n\nExplanation text with $\\LaTeX$ math support.",
  "range": [[-4, 4], [-4, 4], [-4, 4]],
  "camera": { "position": [2, 2, 3], "target": [0, 0, 0] },
  "views": [],
  "elements": [],
  "steps": []
}
```

**Scene fields:**

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | Shown in the title bar |
| `description` | string | Short subtitle shown in UI |
| `markdown` | string | Full lesson text, rendered in the Doc panel. Supports LaTeX math (`$...$`) and Markdown. |
| `range` | `[[xmin,xmax],[ymin,ymax],[zmin,zmax]]` | Axis bounds |
| `camera` | `{position, target}` | Initial camera position |
| `elements` | array | Objects to display (vectors, planes, points, etc.) |
| `steps` | array | Progressive reveal steps for narrated walkthroughs |

**Element types:**

```json
{ "type": "vector", "id": "v1", "from": [0,0,0], "to": [1,2,0], "color": "#ff4444", "label": "v₁" }
{ "type": "point",  "id": "p1", "position": [1,1,0], "color": "#44ff44", "label": "A" }
{ "type": "plane",  "id": "pl", "normal": [0,0,1], "point": [0,0,0], "color": "#4488ff" }
{ "type": "line",   "id": "l1", "from": [-3,0,0], "to": [3,0,0], "color": "#ffffff" }
```

**Steps (progressive reveal):**

```json
{
  "steps": [
    {
      "caption": "Start with a vector **v**.",
      "add": ["v1"],
      "remove": [],
      "sliders": []
    },
    {
      "caption": "Now add a second vector **w**.",
      "add": ["v2"],
      "remove": [],
      "sliders": []
    }
  ]
}
```

Use the built-in scenes in `scenes/` as reference — `eigenvalues.json` and `matrix-transformations.json` show sliders and animated elements.

**Tips:**
- `caption` supports Markdown and LaTeX math
- Use `"remove": ["*"]` to clear all elements in a step
- Sliders: add `"sliders": [{"id": "t", "label": "t", "min": 0, "max": 1, "value": 0.5}]`
- Animated sliders: add `"animate": true, "duration": 2000` to a slider def

---

### Adding a Voice Character

Voice characters are defined in [`gemini-live-tools`](https://github.com/ibenian/gemini-live-tools) — open a PR there.

**Two things to add in `python/gemini_live_tools/gemini_live_api.py`:**

**1. The character description** in the `CHARACTERS` dict:

```python
CHARACTERS: Dict[str, str] = {
    # ... existing characters ...
    "my_character": "My character: brief style description for Gemini to follow.",
}
```

Keep it one sentence. Be specific about cadence, tone, and any accent. Examples from existing characters:
- `"feynman"`: *"Feynman explainer: radically simple, builds intuition from first principles, uses everyday analogies, zero jargon unless earned."*
- `"rubber_duck"`: *"Rubber duck debugger: talks through the problem slowly out loud, restates assumptions, catches obvious mistakes."*
- `"oxford"`: *"Oxford professor: formal British cadence, precise diction, scholarly and composed."*

**2. The default voice** in the `CHARACTER_DEFAULT_VOICES` dict:

```python
CHARACTER_DEFAULT_VOICES: Dict[str, str] = {
    # ... existing entries ...
    "my_character": "Kore",   # pick a Gemini voice that fits
}
```

Available Gemini voices: `Kore`, `Charon`, `Fenrir`, `Aoede`, `Puck`, `Leda`, `Orus`, `Zephyr`,
`Iapetus`, `Gacrux`, `Rasalgethi`, `Achird`, `Alnilam`, `Algenib`, `Erinome`, `Achernar`,
`Sadaltager`, `Autonoe`, `Callirrhoe`, `Laomedeia`, `Sadachbia`, `Sulafat`, `Schedar`,
`Despina`, `Umbriel`, `Pulcherrima`.

**3. The UI group** in `static/voice-character-selector.js`:

```js
const CHARACTER_GROUPS = {
    // ...
    my_character: 'Core',  // groups: Core, Academic, Accents, Character, Dramatic, Musical, Fiction
};
```

That's it — open a PR and the character will appear in the voice picker immediately.

---

## Project Structure

```
mathboxAI/
├── mathboxAI          Python server (run this)
├── app.js             3D scene rendering, sliders, camera
├── chat.js            AI chat panel, TTS, voice picker
├── scenes/            Lesson JSON files (contribute here!)
│   ├── eigenvalues.json
│   ├── matrix-transformations.json
│   ├── vector-operations.json
│   └── ...
└── static/
    └── voice-character-selector.js   Voice/character picker UI widget
```

---

## License

MIT
