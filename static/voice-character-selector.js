(function (global) {
    'use strict';

    const CHARACTER_GROUPS = {
        crisp: 'Core',
        casual: 'Core',
        mentor: 'Core',
        socratic: 'Core',
        first_principles: 'Core',
        feynman: 'Academic',
        visualizer: 'Core',
        debugger: 'Core',
        architect: 'Core',
        product: 'Core',
        skeptic: 'Core',
        professor: 'Academic',
        oxford: 'Academic',
        wolfram: 'Academic',
        gorard: 'Academic',
        curious: 'Academic',
        aussie: 'Accents',
        russian: 'Accents',
        turkish: 'Accents',
        duck: 'Character',
        rubber_duck: 'Character',
        joker: 'Character',
        showman: 'Character',
        drama_queen: 'Character',
        overconfident: 'Character',
        junior: 'Character',
        enthusiast: 'Character',
        narrator: 'Dramatic',
        documentary_40s: 'Dramatic',
        dark_knight: 'Dramatic',
        horror: 'Dramatic',
        dylan_thomas: 'Dramatic',
        news_anchor: 'Dramatic',
        poetic: 'Musical',
        singer: 'Musical',
        rapper: 'Musical',
        monk: 'Character',
        coding_zen: 'Character',
        conspiracy: 'Character',
        kids_tv: 'Character',
        midwest_nice: 'Character',
        french_philosopher: 'Academic',
        schmidhuber: 'Academic',
        yoda: 'Fiction',
        hal9000: 'Fiction',
        captain_upspeak: 'Fiction',
        sailor: 'Fiction',
        cowboy: 'Fiction',
        code_monkey: 'Fiction',
        speedrun: 'Speed',
        giggly: 'Core',
        investigator: 'Core',
        storyteller: 'Core'
    };

    const CHARACTER_GROUP_ORDER = [
        'Recent',
        'Core',
        'Academic',
        'Accents',
        'Character',
        'Dramatic',
        'Musical',
        'Fiction',
        'Speed',
        'Other'
    ];

    const CHARACTER_OPTIONS = [
        { id: 'crisp', label: 'Crisp Engineer', defaultVoice: 'Kore' },
        { id: 'casual', label: 'Casual', defaultVoice: 'Achird' },
        { id: 'mentor', label: 'Mentor', defaultVoice: 'Charon' },
        { id: 'socratic', label: 'Socratic Guide', defaultVoice: 'Iapetus' },
        { id: 'first_principles', label: 'First Principles', defaultVoice: 'Kore' },
        { id: 'feynman', label: 'Feynman Explainer', defaultVoice: 'Puck' },
        { id: 'visualizer', label: 'Visualizer', defaultVoice: 'Laomedeia' },
        { id: 'giggly', label: 'Giggly', defaultVoice: 'Leda' },
        { id: 'professor', label: 'Professor', defaultVoice: 'Iapetus' },
        { id: 'oxford', label: 'Oxford Professor', defaultVoice: 'Rasalgethi' },
        { id: 'aussie', label: 'Australian Coder', defaultVoice: 'Achird' },
        { id: 'russian', label: 'Russian Coder', defaultVoice: 'Alnilam' },
        { id: 'turkish', label: 'Turkish Coder', defaultVoice: 'Erinome' },
        { id: 'narrator', label: 'Documentary Narrator', defaultVoice: 'Gacrux' },
        { id: 'documentary_40s', label: '40s Documentary Narrator', defaultVoice: 'Schedar' },
        { id: 'captain_upspeak', label: 'Captain Upspeak', defaultVoice: 'Puck' },
        { id: 'code_monkey', label: 'Code Monkey', defaultVoice: 'Fenrir' },
        { id: 'horror', label: 'Horror Storyteller', defaultVoice: 'Umbriel' },
        { id: 'poetic', label: 'Poetic Opera', defaultVoice: 'Pulcherrima' },
        { id: 'singer', label: 'Singer', defaultVoice: 'Aoede' },
        { id: 'rapper', label: 'Rapper', defaultVoice: 'Fenrir' },
        { id: 'sailor', label: 'Drunk Sailor', defaultVoice: 'Algenib' },
        { id: 'cowboy', label: 'Southern Cowboy', defaultVoice: 'Orus' },
        { id: 'duck', label: 'Cartoon Duck', defaultVoice: 'Zephyr' },
        { id: 'rubber_duck', label: 'Rubber Duck Debugger', defaultVoice: 'Achird' },
        { id: 'dark_knight', label: 'Dark Knight', defaultVoice: 'Algenib' },
        { id: 'yoda', label: 'Yoda-like', defaultVoice: 'Iapetus' },
        { id: 'hal9000', label: 'HAL 9000', defaultVoice: 'Charon' },
        { id: 'investigator', label: 'Investigator', defaultVoice: 'Erinome' },
        { id: 'product', label: 'Product-Minded', defaultVoice: 'Kore' },
        { id: 'skeptic', label: 'Skeptic', defaultVoice: 'Orus' },
        { id: 'storyteller', label: 'Storyteller', defaultVoice: 'Callirrhoe' },
        { id: 'debugger', label: 'Debugger', defaultVoice: 'Charon' },
        { id: 'architect', label: 'Architect', defaultVoice: 'Rasalgethi' },
        { id: 'monk', label: 'Zen Monk', defaultVoice: 'Achernar' },
        { id: 'coding_zen', label: 'Coding Zen', defaultVoice: 'Achernar' },
        { id: 'speedrun', label: 'Speedrun', defaultVoice: 'Puck' },
        { id: 'enthusiast', label: 'Enthusiast', defaultVoice: 'Laomedeia' },
        { id: 'overconfident', label: 'Overconfident Eng', defaultVoice: 'Sadaltager' },
        { id: 'junior', label: 'Junior Engineer', defaultVoice: 'Leda' },
        { id: 'showman', label: 'Showman', defaultVoice: 'Sadachbia' },
        { id: 'joker', label: 'Joker', defaultVoice: 'Puck' },
        { id: 'conspiracy', label: 'Conspiracy Theorist', defaultVoice: 'Algenib' },
        { id: 'drama_queen', label: 'Drama Queen', defaultVoice: 'Pulcherrima' },
        { id: 'news_anchor', label: 'News Anchor', defaultVoice: 'Sadaltager' },
        { id: 'wolfram', label: 'Wolfram', defaultVoice: 'Iapetus' },
        { id: 'gorard', label: 'Gorard', defaultVoice: 'Rasalgethi' },
        { id: 'dylan_thomas', label: 'Dylan Thomas', defaultVoice: 'Gacrux' },
        { id: 'curious', label: 'Curious', defaultVoice: 'Autonoe' },
        { id: 'kids_tv', label: 'Kids TV Host', defaultVoice: 'Zephyr' },
        { id: 'midwest_nice', label: 'Midwest Nice', defaultVoice: 'Sulafat' },
        { id: 'french_philosopher', label: 'French Philosopher', defaultVoice: 'Despina' },
        { id: 'schmidhuber', label: 'Schmidhuber', defaultVoice: 'Alnilam' }
    ];

    const GEMINI_VOICES = [
        'Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede',
        'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba',
        'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar',
        'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi',
        'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat'
    ];

    function scoreMatch(query, text) {
        if (!query) return 1;
        if (!text) return 0;
        const q = query.toLowerCase();
        const t = text.toLowerCase();
        if (t.startsWith(q)) return 3;
        if (t.includes(q)) return 2;
        return 0;
    }

    class CharacterPicker {
        constructor(opts) {
            this.buttonEl = opts.buttonEl;
            this.paletteEl = opts.paletteEl;
            this.searchEl = opts.searchEl;
            this.listEl = opts.listEl;
            this.backdropEl = opts.backdropEl;
            this.options = opts.options || CHARACTER_OPTIONS;
            this.groupMap = opts.groupMap || CHARACTER_GROUPS;
            this.groupOrder = opts.groupOrder || CHARACTER_GROUP_ORDER;
            this.storageKey = opts.storageKey || 'aiStyle';
            this.recentsKey = opts.recentsKey || 'styleRecents';
            this.defaultId = opts.defaultId || (this.options[0] && this.options[0].id);
            this.hotkey = opts.hotkey || 'k';
            this.onChange = opts.onChange || function () {};
            this.styleItemsVisible = [];
            this.activeStyleIndex = -1;
            this.currentId = this.defaultId;
        }

        init() {
            const saved = localStorage.getItem(this.storageKey);
            this.currentId = saved || this.defaultId;
            this.updateButton(this.currentId);
            this.bindEvents();
            this.onChange(this.currentId);
            return this.currentId;
        }

        getValue() {
            return this.currentId;
        }

        setValue(styleId, opts = {}) {
            const closePalette = opts.closePalette !== false;
            const saveRecent = opts.saveRecent !== false;
            if (!styleId) {
                if (closePalette) this.close();
                return;
            }
            this.currentId = styleId;
            localStorage.setItem(this.storageKey, styleId);
            if (saveRecent) this.storeRecentStyle(styleId);
            this.updateButton(styleId);
            this.onChange(styleId);
            if (closePalette) this.close();
        }

        isOpen() {
            return !this.paletteEl.hidden;
        }

        isFocused(target = null) {
            if (!this.isOpen()) return false;
            const active = target || document.activeElement;
            return !!(active && this.paletteEl.contains(active));
        }

        open() {
            this.positionPalette();
            this.backdropEl.hidden = false;
            this.paletteEl.hidden = false;
            this.searchEl.value = '';
            this.render('');
            setTimeout(() => this.searchEl.focus(), 0);
        }

        close() {
            this.backdropEl.hidden = true;
            this.paletteEl.hidden = true;
            this.searchEl.value = '';
        }

        toggle() {
            if (this.isOpen()) this.close();
            else this.open();
        }

        getRecentStyles() {
            try {
                const raw = localStorage.getItem(this.recentsKey);
                const parsed = raw ? JSON.parse(raw) : [];
                return Array.isArray(parsed) ? parsed : [];
            } catch (e) {
                return [];
            }
        }

        storeRecentStyle(styleId) {
            const recents = this.getRecentStyles().filter((id) => id !== styleId);
            recents.unshift(styleId);
            localStorage.setItem(this.recentsKey, JSON.stringify(recents.slice(0, 6)));
        }

        updateButton(styleId) {
            const item = this.options.find((opt) => opt.id === styleId);
            const label = item ? item.label : styleId;
            const group = (item && item.group) || this.groupMap[styleId] || 'Other';
            this.buttonEl.textContent = `${label} - ${group}`;
            this.buttonEl.title = `AI style (${group})`;
        }

        applyActiveIndex(index) {
            if (!this.styleItemsVisible.length) {
                this.activeStyleIndex = -1;
                return;
            }
            const bounded = Math.max(0, Math.min(index, this.styleItemsVisible.length - 1));
            this.activeStyleIndex = bounded;
            this.styleItemsVisible.forEach((item, idx) => {
                if (idx === bounded) {
                    item.el.classList.add('active');
                    item.el.scrollIntoView({ block: 'nearest' });
                } else {
                    item.el.classList.remove('active');
                }
            });
        }

        render(query) {
            const q = (query || '').trim().toLowerCase();
            const styles = this.options.map((item) => ({
                ...item,
                group: item.group || this.groupMap[item.id] || 'Other'
            }));

            const filtered = styles
                .map((style) => {
                    const score = Math.max(
                        scoreMatch(q, style.label),
                        scoreMatch(q, style.id),
                        scoreMatch(q, style.group)
                    );
                    return { ...style, score };
                })
                .filter((style) => style.score > 0)
                .sort((a, b) => {
                    if (b.score !== a.score) return b.score - a.score;
                    return a.label.localeCompare(b.label);
                });

            const recents = this.getRecentStyles();
            const recentItems = recents
                .map((id) => styles.find((s) => s.id === id))
                .filter(Boolean)
                .filter((item) => !q || item.label.toLowerCase().includes(q) || item.id.toLowerCase().includes(q));

            const grouped = {};
            if (recentItems.length) grouped.Recent = recentItems;
            filtered.forEach((item) => {
                const group = item.group || 'Other';
                if (!grouped[group]) grouped[group] = [];
                grouped[group].push(item);
            });

            this.listEl.innerHTML = '';
            this.styleItemsVisible = [];

            const groups = this.groupOrder
                .filter((group) => grouped[group] && grouped[group].length)
                .concat(Object.keys(grouped).filter((g) => !this.groupOrder.includes(g)));

            if (!groups.length) {
                const empty = document.createElement('div');
                empty.className = 'style-empty';
                empty.textContent = 'No styles match your search.';
                this.listEl.appendChild(empty);
                return;
            }

            groups.forEach((group) => {
                const section = document.createElement('div');
                section.className = 'style-group';

                const title = document.createElement('div');
                title.className = 'style-group-title';
                title.textContent = group;
                section.appendChild(title);

                grouped[group].forEach((item) => {
                    const row = document.createElement('div');
                    row.className = 'style-item';
                    row.dataset.styleId = item.id;

                    const label = document.createElement('div');
                    label.className = 'style-item-label';
                    label.textContent = item.label;

                    const badge = document.createElement('div');
                    badge.className = 'style-item-badge';
                    badge.textContent = item.group || 'Other';

                    row.appendChild(label);
                    row.appendChild(badge);
                    section.appendChild(row);
                    this.styleItemsVisible.push({ id: item.id, el: row });
                });

                this.listEl.appendChild(section);
            });

            const currentIndex = this.styleItemsVisible.findIndex((item) => item.id === this.currentId);
            this.applyActiveIndex(currentIndex === -1 ? 0 : currentIndex);
        }

        positionPalette() {
            const rect = this.buttonEl.getBoundingClientRect();
            const paletteWidth = 320;
            const paletteHeight = 320;
            const gap = 8;
            let left = rect.right - paletteWidth;
            let top = rect.bottom + gap;
            const maxLeft = window.innerWidth - paletteWidth - 8;
            const maxTop = window.innerHeight - paletteHeight - 8;
            if (left < 8) left = 8;
            if (left > maxLeft) left = maxLeft;
            if (top > maxTop) top = Math.max(8, rect.top - paletteHeight - gap);
            this.paletteEl.style.left = `${left}px`;
            this.paletteEl.style.top = `${top}px`;
        }

        bindEvents() {
            this.buttonEl.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggle();
            });

            this.backdropEl.addEventListener('mousedown', () => this.close());

            this.searchEl.addEventListener('input', (e) => {
                this.render(e.target.value || '');
            });

            this.listEl.addEventListener('mousedown', (e) => {
                const item = e.target.closest('.style-item');
                if (!item) return;
                e.preventDefault();
                this.setValue(item.dataset.styleId);
            });

            this.searchEl.addEventListener('keydown', (e) => {
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.applyActiveIndex(this.activeStyleIndex + 1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.applyActiveIndex(this.activeStyleIndex - 1);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    const active = this.styleItemsVisible[this.activeStyleIndex];
                    if (active) this.setValue(active.id);
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    this.close();
                }
            });

            document.addEventListener('click', (e) => {
                if (this.isOpen() && !this.paletteEl.contains(e.target) && !this.buttonEl.contains(e.target)) {
                    this.close();
                }
            });

            document.addEventListener('focusin', (e) => {
                if (this.isOpen() && !this.paletteEl.contains(e.target) && !this.buttonEl.contains(e.target)) {
                    this.close();
                }
            });

            document.addEventListener('mousedown', (e) => {
                if (this.isOpen() && !this.paletteEl.contains(e.target) && !this.buttonEl.contains(e.target)) {
                    this.close();
                }
            }, true);

            this.paletteEl.addEventListener('focusout', (e) => {
                if (!this.isOpen()) return;
                const next = e.relatedTarget;
                if (!next || !this.paletteEl.contains(next)) {
                    setTimeout(() => {
                        if (!this.isFocused()) this.close();
                    }, 0);
                }
            });

            document.addEventListener('keydown', (e) => {
                const isCmdK = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === this.hotkey;
                if (this.isFocused(e.target)) return;
                if (isCmdK) {
                    e.preventDefault();
                    this.open();
                }
            });

            window.addEventListener('resize', () => {
                if (this.isOpen()) this.positionPalette();
            });
            window.addEventListener('blur', () => {
                if (this.isOpen()) this.close();
            });
            window.addEventListener('scroll', () => {
                if (this.isOpen()) this.positionPalette();
            }, true);
        }
    }

    function setupVoiceSelect(selectEl, opts = {}) {
        const includeSystem = !!opts.includeSystem;
        const storageKey = opts.storageKey;
        const defaultValue = opts.defaultValue || (includeSystem ? 'system' : 'Charon');

        selectEl.innerHTML = '';
        if (includeSystem) {
            const systemOpt = document.createElement('option');
            systemOpt.value = 'system';
            systemOpt.textContent = 'System TTS';
            selectEl.appendChild(systemOpt);
        }
        GEMINI_VOICES.forEach((voice) => {
            const opt = document.createElement('option');
            opt.value = voice;
            opt.textContent = voice;
            selectEl.appendChild(opt);
        });

        const stored = storageKey ? localStorage.getItem(storageKey) : null;
        const selected = stored && Array.from(selectEl.options).some((o) => o.value === stored) ? stored : defaultValue;
        selectEl.value = selected;

        if (storageKey) {
            selectEl.addEventListener('change', () => {
                localStorage.setItem(storageKey, selectEl.value);
            });
            localStorage.setItem(storageKey, selectEl.value);
        }

        return selectEl.value;
    }

    global.GeminiVoiceCharacterSelector = {
        CHARACTER_GROUPS,
        CHARACTER_GROUP_ORDER,
        CHARACTER_OPTIONS,
        GEMINI_VOICES,
        CharacterPicker,
        setupVoiceSelect
    };
})(window);
