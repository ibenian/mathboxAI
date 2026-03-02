# mathboxAI

Interactive 3D math visualizer built on [MathBox](https://github.com/unconed/mathbox), with AI chat and narrated lessons — powered by Gemini.

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

To update to the latest version of [`gemini-live-tools`](https://github.com/ibenian/gemini-live-tools) (which includes new voice characters and the voice picker UI):

```bash
./mathboxAI --update
```

This reinstalls `gemini-live-tools` from GitHub and copies the updated `voice-character-selector.js` into the app. Not ideal, but simple enough for now.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add scenes, voice characters, and more.

---

## Project Structure

```
mathboxAI/
├── mathboxAI          Launcher (run this)
├── server.py          Python server
├── scenes/            Lesson JSON files (contribute here!)
│   ├── eigenvalues.json
│   ├── matrix-transformations.json
│   ├── vector-operations.json
│   └── ...
└── static/
    ├── app.js         3D scene rendering, sliders, camera
    ├── chat.js        AI chat panel, TTS, voice picker
    ├── index.html
    └── style.css
```

---

## License

[MIT](LICENSE)

## Disclaimer

This software is provided for educational and informational purposes only. The authors and contributors make no representations or warranties regarding the accuracy, completeness, or suitability of this software for any particular purpose. Use is entirely at your own risk. The authors shall not be held liable for any direct, indirect, incidental, special, or consequential damages arising from the use of or inability to use this software.
