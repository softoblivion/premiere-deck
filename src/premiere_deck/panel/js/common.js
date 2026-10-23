/*
 * Premiere Deck panel: shared client code of both extensions (docs/PANEL.md 1, docs/PROTOCOL.md 1-3).
 * Discovery (deck.json), the WebSocket client with reconnect and backoff, the handshake, logging to the host.
 * Plain ES2015, no Node. The pure helpers are exported for `node tests/panel/run.js`.
 */
(function (root) {
    "use strict";

    var PROTOCOL = 1;
    var RETRY_FAST_MS = 3000;
    var RETRY_SLOW_MS = 15000;
    var FAST_PHASE_MS = 60000;
    // While waiting for the next attempt the client re-reads deck.json this often and connects at once when the
    // port or token changed: a restarted host is joined within this interval of writing the file (PROTOCOL 2).
    var DECK_POLL_MS = 500;
    var LOG_BUFFER = 50;

    // --- pure helpers ------------------------------------------------------------------------------------------

    // Delay before the next connection attempt, from the time the client has been trying (PROTOCOL 2).
    function retryDelay(tryingForMs) {
        return tryingForMs < FAST_PHASE_MS ? RETRY_FAST_MS : RETRY_SLOW_MS;
    }

    // Parses the text of deck.json; returns null for anything that is not a usable discovery file.
    function parseDeckJson(text) {
        var data;
        try {
            data = JSON.parse(text);
        } catch (e) {
            return null;
        }
        if (!data || typeof data !== "object") {
            return null;
        }
        var port = Number(data.port);
        if (!(port > 0 && port < 65536) || typeof data.token !== "string" || !data.token) {
            return null;
        }
        return {
            port: port,
            token: data.token,
            host_version: String(data.host_version || ""),
            protocol: Number(data.protocol || 0),
            host_exe: typeof data.host_exe === "string" && data.host_exe ? data.host_exe : null,
            autostart_host: data.autostart_host === true,
            written_at: Number(data.written_at || 0)
        };
    }

    // True when the freshly read deck.json names another host run than the one the last attempt used (port or
    // token differ). A missing file is not a change; a file after none is.
    function deckChanged(used, fresh) {
        if (!fresh) {
            return false;
        }
        if (!used) {
            return true;
        }
        return used.port !== fresh.port || used.token !== fresh.token;
    }

    // welcome.update = reload at most once per RELOAD_GUARD_MS: a host that keeps asking (versions still differ after
    // the reload) would otherwise reload the page in a loop. The last reload time lives in sessionStorage, which
    // survives location.reload().
    var RELOAD_GUARD_MS = 30000;
    var RELOAD_KEY = "deck.reloaded_at";

    function shouldReload(lastReloadAt, now) {
        return !(lastReloadAt > 0 && now - lastReloadAt < RELOAD_GUARD_MS);
    }

    function reloadAllowed(now) {
        var last = 0;
        try {
            last = Number(root.sessionStorage.getItem(RELOAD_KEY)) || 0;
            if (!shouldReload(last, now)) {
                return false;
            }
            root.sessionStorage.setItem(RELOAD_KEY, String(now));
        } catch (e) {
            return true;
        }
        return true;
    }

    function socketUrl(deck, role) {
        return "ws://127.0.0.1:" + deck.port + "/ws?role=" + encodeURIComponent(role) +
            "&token=" + encodeURIComponent(deck.token);
    }

    function joinPath(folder, name) {
        if (!folder) {
            return name;
        }
        var last = folder.charAt(folder.length - 1);
        return (last === "/" || last === "\\") ? folder + name : folder + "/" + name;
    }

    // Hello message of PROTOCOL 3.
    function helloMessage(info) {
        return {
            type: "hello",
            role: info.role,
            panel_version: info.panel_version || "",
            protocol: PROTOCOL,
            premiere_version: info.premiere_version || "",
            cep_version: info.cep_version || "",
            extension_id: info.extension_id || "",
            project_path: info.project_path || null
        };
    }

    // --- CEP access (absent under Node) ------------------------------------------------------------------------

    function csInterface() {
        if (!root.__deckCs && typeof root.CSInterface === "function") {
            root.__deckCs = new root.CSInterface();
        }
        return root.__deckCs || null;
    }

    function extensionDir() {
        var cs = csInterface();
        if (!cs || typeof root.SystemPath === "undefined") {
            return "";
        }
        try {
            return cs.getSystemPath(root.SystemPath.EXTENSION);
        } catch (e) {
            return "";
        }
    }

    // Development outside Premiere: the page is opened over http(s) (e.g. `python -m http.server` in the panel
    // folder) and CEP's bridge object is missing. deck.json is then fetched next to the page instead of being read
    // through window.cep.fs. Inside Premiere (file: page, __adobe_cep__ present) nothing of this runs.
    function isDevBrowser() {
        return typeof root.__adobe_cep__ === "undefined" && !!root.location &&
            (root.location.protocol === "http:" || root.location.protocol === "https:") && typeof root.fetch === "function";
    }

    var devDeck = null;

    // Fetches deck.json beside index.html; calls back with the parsed deck or null.
    function fetchDevDeck(done) {
        root.fetch("deck.json", {cache: "no-store"}).then(function (response) {
            return response.ok ? response.text() : null;
        }).then(function (text) {
            devDeck = text === null ? null : parseDeckJson(text);
            done(devDeck);
        }, function () {
            devDeck = null;
            done(null);
        });
    }

    function readText(path) {
        if (!root.cep || !root.cep.fs) {
            return null;
        }
        var result = root.cep.fs.readFile(path);
        if (!result || result.err !== 0) {
            return null;
        }
        return String(result.data);
    }

    function fileExists(path) {
        if (!root.cep || !root.cep.fs) {
            return false;
        }
        var result = root.cep.fs.stat(path);
        return !!result && result.err === 0;
    }

    function readDeck() {
        if (isDevBrowser()) {
            return devDeck;
        }
        var text = readText(joinPath(extensionDir(), "deck.json"));
        return text === null ? null : parseDeckJson(text);
    }

    function panelVersion() {
        var text = readText(joinPath(extensionDir(), "VERSION"));
        return text === null ? "" : text.replace(/\s+$/, "").replace(/^\s+/, "");
    }

    function environment() {
        var info = {premiere_version: "", cep_version: "", extension_id: "", skin: null};
        var cs = csInterface();
        if (!cs) {
            return info;
        }
        try {
            var env = cs.getHostEnvironment();
            info.premiere_version = env && env.appVersion ? String(env.appVersion) : "";
            info.skin = env && env.appSkinInfo ? env.appSkinInfo : null;
        } catch (e) {
            info.premiere_version = "";
        }
        try {
            var api = cs.getCurrentApiVersion();
            info.cep_version = api ? api.major + "." + api.minor : "";
        } catch (e) {
            info.cep_version = "";
        }
        try {
            info.extension_id = cs.getExtensionID();
        } catch (e) {
            info.extension_id = "";
        }
        return info;
    }

    // --- client ------------------------------------------------------------------------------------------------

    /*
     * DeckClient(options): one WebSocket connection to the host for one role.
     *   options.role                  "agent" | "display"
     *   options.projectPath()         current project path for hello (agent), optional
     *   options.onMessage(message)    every message after welcome
     *   options.onState(state, info)  "connecting" | "connected" | "restart_pending" | "protocol" | "offline"
     *   options.onWelcome(welcome)    after a welcome without a reload
     */
    function DeckClient(options) {
        this.options = options;
        this.role = options.role;
        this.socket = null;
        this.deck = null;
        this.state = "connecting";
        this.handshaken = false;
        this.startedAt = Date.now();
        this.connectedOnce = false;
        this.timer = null;
        this.logBuffer = [];
        this.stopped = false;
        this.fetching = false;
    }

    DeckClient.prototype.setState = function (state, info) {
        this.state = state;
        if (this.options.onState) {
            try {
                this.options.onState(state, info || {});
            } catch (e) {
                this.consoleLog("error", "onState failed: " + e.message);
            }
        }
    };

    DeckClient.prototype.start = function () {
        this.stopped = false;
        this.attempt();
    };

    DeckClient.prototype.stop = function () {
        this.stopped = true;
        if (this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
        if (this.socket) {
            try {
                this.socket.close();
            } catch (e) {
                // already closed
            }
        }
    };

    // The next attempt after the retry delay, or as soon as deck.json names another host run (a host restart while
    // Premiere runs: the old token is refused with 4001, the new host has already written its file).
    DeckClient.prototype.scheduleRetry = function () {
        var self = this;
        if (this.stopped || this.timer) {
            return;
        }
        var delay = retryDelay(Date.now() - this.startedAt);
        var due = Date.now() + delay;
        var watchFile = !isDevBrowser();
        var tick = function () {
            self.timer = null;
            if (self.stopped || self.socket) {
                return;
            }
            if (Date.now() >= due || (watchFile && deckChanged(self.deck, readDeck()))) {
                self.attempt();
                return;
            }
            self.timer = setTimeout(tick, Math.min(DECK_POLL_MS, Math.max(0, due - Date.now())));
        };
        this.timer = setTimeout(tick, Math.min(DECK_POLL_MS, delay));
    };

    // Retry right away (the deck.json changed, or the user pressed "Start Premiere Deck").
    DeckClient.prototype.retryNow = function () {
        if (this.timer) {
            clearTimeout(this.timer);
            this.timer = null;
        }
        if (!this.socket) {
            this.attempt();
        }
    };

    DeckClient.prototype.attempt = function () {
        var self = this;
        if (this.stopped || this.socket || this.fetching) {
            return;
        }
        if (isDevBrowser()) {
            this.fetching = true;
            fetchDevDeck(function (deck) {
                self.fetching = false;
                if (!self.stopped && !self.socket) {
                    self.connect(deck);
                }
            });
            return;
        }
        this.connect(readDeck());
    };

    DeckClient.prototype.connect = function (deck) {
        var self = this;
        this.deck = deck;
        if (!deck) {
            this.scheduleRetry();
            return;
        }
        var socket;
        try {
            socket = new WebSocket(socketUrl(deck, this.role));
        } catch (e) {
            this.scheduleRetry();
            return;
        }
        this.socket = socket;
        this.handshaken = false;
        socket.onopen = function () {
            var env = environment();
            var project = null;
            if (self.options.projectPath) {
                try {
                    project = self.options.projectPath();
                } catch (e) {
                    project = null;
                }
            }
            self.sendRaw(helloMessage({
                role: self.role,
                panel_version: panelVersion(),
                premiere_version: env.premiere_version,
                cep_version: env.cep_version,
                extension_id: env.extension_id,
                project_path: project
            }));
        };
        socket.onmessage = function (event) {
            var message;
            try {
                message = JSON.parse(event.data);
            } catch (e) {
                self.consoleLog("warning", "not JSON from host");
                return;
            }
            self.receive(message);
        };
        socket.onclose = function (event) {
            var wasHandshaken = self.handshaken;
            self.socket = null;
            self.handshaken = false;
            if (event && event.code === 4002) {
                self.setState("protocol", {host_version: deck.host_version});
            } else if (self.state !== "restart_pending" || !wasHandshaken) {
                self.setState("connecting", {was_connected: wasHandshaken});
            }
            if (wasHandshaken) {
                // A lost connection starts the fast retry phase again.
                self.startedAt = Date.now();
            }
            self.scheduleRetry();
        };
        socket.onerror = function () {
            // onclose follows and schedules the retry.
        };
    };

    DeckClient.prototype.receive = function (message) {
        if (!message || typeof message.type !== "string") {
            return;
        }
        if (message.type === "welcome") {
            this.handshaken = true;
            this.connectedOnce = true;
            var update = message.update || null;
            if (update && update.action === "reload" && reloadAllowed(Date.now())) {
                root.location.reload();
                return;
            }
            this.flushLogs();
            if (update && update.action === "restart_premiere") {
                this.setState("restart_pending", {host_version: message.host_version});
            } else {
                this.setState("connected", {host_version: message.host_version, session_id: message.session_id});
            }
            if (this.options.onWelcome) {
                this.options.onWelcome(message);
            }
            return;
        }
        if (message.type === "bye") {
            this.setState(message.reason === "protocol" ? "protocol" : "connecting",
                {host_version: this.deck ? this.deck.host_version : ""});
            return;
        }
        if (message.type === "reload") {
            root.location.reload();
            return;
        }
        if (this.options.onMessage) {
            try {
                this.options.onMessage(message);
            } catch (e) {
                this.log("error", "handling " + message.type + " failed: " + e.message);
            }
        }
    };

    DeckClient.prototype.sendRaw = function (message) {
        if (!this.socket || this.socket.readyState !== 1) {
            return false;
        }
        this.socket.send(typeof message === "string" ? message : JSON.stringify(message));
        return true;
    };

    // Sends a message after the handshake; returns false when not connected.
    DeckClient.prototype.send = function (message) {
        if (!this.handshaken) {
            return false;
        }
        return this.sendRaw(message);
    };

    DeckClient.prototype.consoleLog = function (level, message) {
        if (typeof console !== "undefined" && console.log) {
            console.log("[deck " + this.role + "] " + level + ": " + message);
        }
    };

    // Logs to the host (logger panel.<role>); kept in a small buffer while offline.
    DeckClient.prototype.log = function (level, message) {
        this.consoleLog(level, message);
        var entry = {type: "log", level: level, message: String(message)};
        if (!this.send(entry)) {
            this.logBuffer.push(entry);
            if (this.logBuffer.length > LOG_BUFFER) {
                this.logBuffer.shift();
            }
        }
    };

    DeckClient.prototype.flushLogs = function () {
        var pending = this.logBuffer;
        this.logBuffer = [];
        for (var i = 0; i < pending.length; i++) {
            this.sendRaw(pending[i]);
        }
    };

    // Starts the host exe named in deck.json (`args` e.g. ["--from-premiere"]); returns a short status string.
    function startHost(deck, args) {
        if (!deck || !deck.host_exe) {
            return "no_exe";
        }
        if (!fileExists(deck.host_exe)) {
            return "missing";
        }
        if (!root.cep || !root.cep.process || typeof root.cep.process.createProcess !== "function") {
            return "unsupported";
        }
        var callArgs = [deck.host_exe].concat(args || []);
        var result = root.cep.process.createProcess.apply(root.cep.process, callArgs);
        return result && result.err === 0 ? "started" : "failed:" + (result ? result.err : "?");
    }

    var api = {
        PROTOCOL: PROTOCOL,
        retryDelay: retryDelay,
        shouldReload: shouldReload,
        parseDeckJson: parseDeckJson,
        deckChanged: deckChanged,
        DECK_POLL_MS: DECK_POLL_MS,
        socketUrl: socketUrl,
        joinPath: joinPath,
        helloMessage: helloMessage,
        csInterface: csInterface,
        isDevBrowser: isDevBrowser,
        extensionDir: extensionDir,
        readText: readText,
        readDeck: readDeck,
        fileExists: fileExists,
        panelVersion: panelVersion,
        environment: environment,
        startHost: startHost,
        DeckClient: DeckClient
    };

    root.DeckCommon = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
}(typeof window !== "undefined" ? window : this));
