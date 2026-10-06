"use client";

import React, { useState, useEffect, useRef } from "react";

interface CooldownItem {
  activation_id: string;
  phone_number: string;
  requested_at: number;
  remaining_cooldown: number;
  ready: boolean;
}

interface CandidateItem {
  act_id: string;
  phone: string;
  national_number: string;
  prefix: string;
}

interface HistoryItem {
  id: string;
  phone: string;
  prefix: string;
  status: "SUCCESS" | "SKIPPED";
  otp?: string;
  time: string;
}

interface LogEntry {
  timestamp: string;
  level: string;
  message: string;
}

interface EngineConfig {
  api_key_configured: boolean;
  preset: string;
  custom_prefixes: string;
  active_prefixes_count: number;
  max_price: number;
  batch_size: number;
  retry_delay: number;
}

interface EngineState {
  is_running: boolean;
  is_paused: boolean;
  status_message: string;
  balance: number | null;
  active_number: string | null;
  active_id: string | null;
  matched_prefix: string | null;
  queue_count: number;
  candidates: CandidateItem[];
  latest_otp: string | null;
  otps_count: number;
  searches_count: number;
  cooldown_pending: CooldownItem[];
  last_verified?: { number: string; prefix: string; is_valid: boolean; reason: string; time: string; timestamp: number } | null;
  config: EngineConfig;
}

export default function Home() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [state, setState] = useState<EngineState | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [copied, setCopied] = useState(false);
  const [otpCopied, setOtpCopied] = useState(false);
  const [soundEnabled, setSoundEnabled] = useState(true);
  const [showConfig, setShowConfig] = useState(false);
  const [otpAlert, setOtpAlert] = useState<{ otp: string; phone: string; prefix: string } | null>(null);

  // Editable config state
  const [preset, setPreset] = useState("tier1");
  const [maxPrice, setMaxPrice] = useState("0.3");
  const [batchSize, setBatchSize] = useState("2");
  const [customPrefixes, setCustomPrefixes] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [verifyJio, setVerifyJio] = useState(true);
  const [headedBrowser, setHeadedBrowser] = useState(true);
  const [showLiveBrowser, setShowLiveBrowser] = useState(false);
  const [screenshotTimestamp, setScreenshotTimestamp] = useState<number>(Date.now());

  const wsRef = useRef<WebSocket | null>(null);
  const terminalContainerRef = useRef<HTMLDivElement | null>(null);
  const lastActiveNumberRef = useRef<string | null>(null);
  const [autoScrollLogs, setAutoScrollLogs] = useState(true);
  const [soundPlaying, setSoundPlaying] = useState<string | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);

  // Initialize theme with localStorage persistence
  useEffect(() => {
    const saved = localStorage.getItem("jio_theme") as "dark" | "light" | null;
    const initialTheme = saved || "dark";
    setTheme(initialTheme);
    document.documentElement.classList.toggle("dark", initialTheme === "dark");
  }, []);

  const toggleTheme = () => {
    const nextTheme = theme === "dark" ? "light" : "dark";
    setTheme(nextTheme);
    localStorage.setItem("jio_theme", nextTheme);
    document.documentElement.classList.toggle("dark", nextTheme === "dark");
  };

  // Helper to retrieve/create shared AudioContext and resume if suspended
  const getAudioContext = () => {
    if (typeof window === "undefined") return null;
    try {
      const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return null;
      if (!audioCtxRef.current) {
        audioCtxRef.current = new AudioCtx();
      }
      if (audioCtxRef.current.state === "suspended") {
        audioCtxRef.current.resume().catch(() => {});
      }
      return audioCtxRef.current;
    } catch {
      return null;
    }
  };

  // Unlock audio context on initial user tap/click to comply with browser autoplay policies
  useEffect(() => {
    const unlockAudio = () => {
      const ctx = getAudioContext();
      if (ctx && ctx.state === "suspended") {
        ctx.resume().catch(() => {});
      }
      window.removeEventListener("click", unlockAudio);
      window.removeEventListener("touchstart", unlockAudio);
    };
    window.addEventListener("click", unlockAudio, { passive: true });
    window.addEventListener("touchstart", unlockAudio, { passive: true });
    return () => {
      window.removeEventListener("click", unlockAudio);
      window.removeEventListener("touchstart", unlockAudio);
    };
  }, []);

  // Play audio chime when a matching Jio virtual number is found (E5 -> A5 bell chime)
  const playNumberFoundSound = () => {
    if (!soundEnabled) return;
    try {
      const ctx = getAudioContext();
      if (!ctx) return;
      if (ctx.state === "suspended") {
        ctx.resume().catch(() => {});
      }

      const playTone = (freq: number, start: number, dur: number) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, ctx.currentTime + start);
        gain.gain.setValueAtTime(0.28, ctx.currentTime + start);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + start);
        osc.stop(ctx.currentTime + start + dur);
      };

      // Two-tone bell chime: E5 (659Hz) -> A5 (880Hz)
      playTone(659.25, 0, 0.18);
      playTone(880.00, 0.15, 0.45);
    } catch {
      // Audio fallback
    }
  };

  // Play audio chime for OTP via Web Audio API synthesis (D5 -> A5 -> D6 triumph chime)
  const playOtpSound = () => {
    if (!soundEnabled) return;
    try {
      const ctx = getAudioContext();
      if (!ctx) return;
      if (ctx.state === "suspended") {
        ctx.resume().catch(() => {});
      }

      const playTone = (freq: number, start: number, dur: number) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, ctx.currentTime + start);
        gain.gain.setValueAtTime(0.3, ctx.currentTime + start);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + start);
        osc.stop(ctx.currentTime + start + dur);
      };

      playTone(587.33, 0, 0.18); // D5
      playTone(880.00, 0.14, 0.35); // A5
      playTone(1174.66, 0.28, 0.55); // D6
    } catch {
      // Audio fallback
    }
  };

  // WebSocket Connection
  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimer: NodeJS.Timeout;

    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = window.location.hostname;
      const wsUrl = `${protocol}//${host}:8000/ws`;

      ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setWsConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "state_changed") {
            const currentActiveNum = msg.data?.active_number;
            if (
              currentActiveNum &&
              currentActiveNum !== "None" &&
              currentActiveNum !== lastActiveNumberRef.current
            ) {
              lastActiveNumberRef.current = currentActiveNum;
              playNumberFoundSound();
            } else if (!currentActiveNum || currentActiveNum === "None") {
              lastActiveNumberRef.current = null;
            }

            setState(msg.data);
            if (msg.data?.last_verified) {
              setScreenshotTimestamp(Date.now());
            }
            if (msg.data.config) {
              setPreset(msg.data.config.preset || "tier1");
              setMaxPrice(String(msg.data.config.max_price ?? "0.3"));
              setBatchSize(String(msg.data.config.batch_size ?? "2"));
              setCustomPrefixes(msg.data.config.custom_prefixes || "");
              if (msg.data.config.verify_jio !== undefined) setVerifyJio(msg.data.config.verify_jio);
              if (msg.data.config.headed_browser !== undefined) setHeadedBrowser(msg.data.config.headed_browser);
            }
          } else if (msg.type === "initial_logs") {
            setLogs(msg.data || []);
          } else if (msg.type === "log") {
            setLogs((prev) => [...prev.slice(-120), msg.data]);
          } else if (msg.type === "screenshot_updated") {
            setScreenshotTimestamp(Date.now());
          } else if (msg.type === "otp_received") {
            setOtpAlert({
              otp: msg.data.otp,
              phone: msg.data.phone,
              prefix: msg.data.prefix,
            });
            playOtpSound();
          }
        } catch (e) {
          console.error("WS Parse error", e);
        }
      };

      ws.onclose = () => {
        setWsConnected(false);
        reconnectTimer = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    const fetchHistory = async () => {
      try {
        const res = await fetch(`http://${window.location.hostname}:8000/api/history`);
        if (res.ok) {
          const data = await res.json();
          setHistory(data);
        }
      } catch {
        // Ignored
      }
    };
    fetchHistory();
    const histInterval = setInterval(fetchHistory, 5000);

    return () => {
      clearTimeout(reconnectTimer);
      clearInterval(histInterval);
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  // Auto-scroll logs strictly inside terminal container (never hijack browser window scroll)
  useEffect(() => {
    if (autoScrollLogs && terminalContainerRef.current) {
      terminalContainerRef.current.scrollTop = terminalContainerRef.current.scrollHeight;
    }
  }, [logs, autoScrollLogs]);

  // Actions
  const sendAction = async (action: string) => {
    try {
      const host = window.location.hostname;
      await fetch(`http://${host}:8000/api/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
    } catch (e) {
      console.error("Action error", e);
    }
  };

  const handlePresetSelect = async (selectedPreset: string) => {
    setPreset(selectedPreset);
    try {
      const host = window.location.hostname;
      await fetch(`http://${host}:8000/api/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: selectedPreset }),
      });
    } catch (e) {
      console.error("Preset switch error", e);
    }
  };

  const saveConfig = async () => {
    try {
      const host = window.location.hostname;
      const payload: Record<string, unknown> = {
        preset,
        max_price: parseFloat(maxPrice) || 0.3,
        batch_size: parseInt(batchSize) || 2,
        custom_prefixes: customPrefixes,
        verify_jio: verifyJio,
        headed_browser: headedBrowser,
      };
      if (apiKey.trim()) {
        payload.api_key = apiKey.trim();
      }

      const res = await fetch(`http://${host}:8000/api/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        setShowConfig(false);
        setApiKey("");
      }
    } catch (e) {
      console.error("Config save error", e);
    }
  };

  // Robust Universal Copy to Clipboard (works over plain HTTP, mobile browsers, iOS Safari, Android)
  const copyToClipboard = async (rawText: string, isOtp = false) => {
    if (!rawText) return;
    const text = String(rawText).trim();
    let copiedSuccess = false;

    // Method 1: Modern Clipboard API (works on HTTPS and localhost)
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        copiedSuccess = true;
      } catch {
        copiedSuccess = false;
      }
    }

    // Method 2: Universal Fallback for plain HTTP / mobile IP addresses (execCommand)
    if (!copiedSuccess && typeof document !== "undefined") {
      try {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.top = "-9999px";
        textarea.style.left = "-9999px";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        copiedSuccess = document.execCommand("copy");
        document.body.removeChild(textarea);
      } catch (err) {
        console.error("Fallback copy failed:", err);
      }
    }

    // Trigger subtle haptic vibration on mobile devices if supported
    if (typeof navigator !== "undefined" && navigator.vibrate) {
      try {
        navigator.vibrate(40);
      } catch {
        // Ignored
      }
    }

    if (isOtp) {
      setOtpCopied(true);
      setTimeout(() => setOtpCopied(false), 2000);
    } else {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="min-h-screen flex flex-col font-sans transition-colors duration-200" style={{ backgroundColor: "var(--bg-main)", color: "var(--text-primary)" }}>
      {/* Sticky App Shell Topbar */}
      <header
        className="px-4 py-3 sticky top-0 z-40 border-b backdrop-blur-md transition-colors duration-200"
        style={{
          backgroundColor: "var(--bg-nav)",
          borderColor: "var(--border-color)",
        }}
      >
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-[#1E40AF] text-white flex items-center justify-center font-bold text-xs tracking-wider shadow-sm">
              JIO
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="font-semibold text-sm tracking-tight" style={{ color: "var(--text-primary)" }}>
                  Grizzly MyJio Automator
                </h1>
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-slate-100 text-slate-600 border border-slate-200 dark:bg-zinc-800 dark:text-zinc-400 dark:border-zinc-700">
                  v2.0
                </span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                {wsConnected ? (
                  <span className="inline-flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400 font-medium">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                    Live Connected
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-xs text-rose-700 dark:text-rose-400 font-medium">
                    <span className="w-1.5 h-1.5 rounded-full bg-rose-500"></span>
                    Connecting...
                  </span>
                )}
                <span style={{ color: "var(--text-muted)" }}>•</span>
                <span style={{ color: "var(--text-secondary)" }}>
                  {state?.config?.preset.toUpperCase()} ({state?.config?.active_prefixes_count} prefixes)
                </span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            {/* Balance Widget */}
            <div
              className="px-3 py-1.5 rounded-lg border flex items-center gap-2 shadow-xs transition-colors duration-200"
              style={{
                backgroundColor: "var(--bg-card)",
                borderColor: "var(--border-color)",
              }}
            >
              <span className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: "var(--text-muted)" }}>
                Balance
              </span>
              <span className="text-sm font-semibold font-mono text-emerald-700 dark:text-emerald-400">
                {state?.balance !== null && state?.balance !== undefined
                  ? `$${state.balance.toFixed(2)}`
                  : "$--"}
              </span>
            </div>

            {/* Light / Dark Mode Toggle Button */}
            <button
              onClick={toggleTheme}
              title={theme === "dark" ? "Switch to Light Mode" : "Switch to Dark Mode"}
              className="p-2 rounded-lg border tactile-btn flex items-center justify-center transition-colors duration-200 bg-white hover:bg-slate-100 border-slate-300 text-slate-700 dark:bg-zinc-900 dark:border-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              {theme === "dark" ? (
                // Sun Icon (Switch to Light)
                <svg className="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                </svg>
              ) : (
                // Moon Icon (Switch to Dark)
                <svg className="w-4 h-4 text-slate-700" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                </svg>
              )}
            </button>

            {/* Audio Chime Toggle */}
            <button
              onClick={() => setSoundEnabled(!soundEnabled)}
              title={soundEnabled ? "Mute chimes" : "Unmute chimes"}
              className={`p-2 rounded-lg border tactile-btn flex items-center justify-center transition-colors duration-200 ${
                soundEnabled
                  ? "bg-white hover:bg-slate-100 border-slate-300 text-slate-700 dark:bg-zinc-900 dark:border-zinc-800 dark:text-zinc-200"
                  : "bg-slate-100 border-slate-200 text-slate-400 dark:bg-zinc-950 dark:border-zinc-900 dark:text-zinc-600"
              }`}
            >
              {soundEnabled ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M17 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2" />
                </svg>
              )}
            </button>

            {/* Settings Drawer Button */}
            <button
              onClick={() => setShowConfig(!showConfig)}
              className="p-2 rounded-lg border tactile-btn transition-colors duration-200 bg-white hover:bg-slate-100 border-slate-300 text-slate-700 dark:bg-zinc-900 dark:border-zinc-800 dark:text-zinc-200 dark:hover:bg-zinc-800"
              title="Settings"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </button>
          </div>
        </div>
      </header>

      {/* Main Viewport */}
      <main className="max-w-6xl w-full mx-auto p-4 sm:p-6 space-y-6 flex-1">
        {/* Mitra UI Range Pill Tabs: Prefix Selection */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div className="p-1 rounded-lg bg-slate-200/80 dark:bg-zinc-900 border border-slate-300 dark:border-zinc-800 inline-flex items-center gap-1 shadow-xs">
            <button
              onClick={() => handlePresetSelect("tier1")}
              className={`px-3 py-1.5 text-xs font-semibold rounded-md tactile-btn transition-all ${
                preset === "tier1"
                  ? "bg-white text-slate-900 shadow-sm dark:bg-zinc-800 dark:text-zinc-100"
                  : "text-slate-600 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-200"
              }`}
            >
              Tier 1 (Strict: 5 Prefixes)
            </button>
            <button
              onClick={() => handlePresetSelect("tier2")}
              className={`px-3 py-1.5 text-xs font-semibold rounded-md tactile-btn transition-all ${
                preset === "tier2"
                  ? "bg-white text-slate-900 shadow-sm dark:bg-zinc-800 dark:text-zinc-100"
                  : "text-slate-600 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-200"
              }`}
            >
              Tier 2 (Popular: 35 Prefixes)
            </button>
            <button
              onClick={() => handlePresetSelect("tier3")}
              className={`px-3 py-1.5 text-xs font-semibold rounded-md tactile-btn transition-all ${
                preset === "tier3"
                  ? "bg-white text-slate-900 shadow-sm dark:bg-zinc-800 dark:text-zinc-100"
                  : "text-slate-600 hover:text-slate-900 dark:text-zinc-400 dark:hover:text-zinc-200"
              }`}
            >
              Tier 3 (Extended: 82 Prefixes)
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500 dark:text-zinc-400 font-medium">Service:</span>
              <span className="status-pill status-neutral">
                Jio Cellular LTE (+91)
              </span>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-500 dark:text-zinc-400 font-medium">Validator:</span>
              {verifyJio ? (
                <span className="status-pill status-info flex items-center gap-1.5 font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"></span>
                  {headedBrowser ? "Playwright (Visual Chrome)" : "Playwright (Headless)"}
                </span>
              ) : (
                <span className="status-pill status-neutral font-medium">
                  Manual Chrome Entry
                </span>
              )}
            </div>
          </div>
        </div>

        {/* OTP Received Alert Banner */}
        {otpAlert && (
          <div className="panel-card border border-emerald-300 dark:border-emerald-800/60 bg-emerald-50 dark:bg-emerald-950/30 p-5 shadow-sm">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-4">
              <div className="flex items-center gap-3.5">
                <div className="w-10 h-10 rounded-lg bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-400 flex items-center justify-center font-bold">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wider text-emerald-800 dark:text-emerald-400">
                    OTP Code Received for +91 {otpAlert.phone}
                  </p>
                  <p className="text-3xl sm:text-4xl font-bold font-mono tracking-widest text-emerald-950 dark:text-emerald-300">
                    {otpAlert.otp}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-2 w-full sm:w-auto">
                <button
                  onClick={() => copyToClipboard(otpAlert.otp, true)}
                  className="flex-1 sm:flex-initial px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-xs tactile-btn flex items-center justify-center gap-2 shadow-xs"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                  </svg>
                  {otpCopied ? "Copied to Clipboard" : "Copy OTP Code"}
                </button>
                <button
                  onClick={() => setOtpAlert(null)}
                  className="px-3.5 py-2 rounded-lg border border-emerald-300 dark:border-emerald-800 bg-white dark:bg-emerald-900/20 text-emerald-800 dark:text-emerald-300 font-medium text-xs tactile-btn"
                >
                  Dismiss
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Settings Drawer Panel */}
        {showConfig && (
          <div className="panel-card space-y-4">
            <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border-color)" }}>
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center text-blue-700 dark:text-blue-400">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
                  </svg>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wider text-slate-800 dark:text-zinc-200">
                    Configuration Parameters
                  </p>
                  <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                    Adjust target prefix allocations, budget thresholds, and worker concurrency.
                  </p>
                </div>
              </div>
              <button
                onClick={() => setShowConfig(false)}
                className="text-xs font-medium text-slate-500 hover:text-slate-800 dark:text-zinc-400 dark:hover:text-zinc-200"
              >
                Close
              </button>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <div>
                <label className="text-xs font-semibold uppercase tracking-wider block mb-1.5 text-slate-600 dark:text-zinc-400">
                  Target Preset
                </label>
                <select
                  value={preset}
                  onChange={(e) => setPreset(e.target.value)}
                  className="field"
                >
                  <option value="tier1">Tier 1 (Strict: 600, 939, 700, 701, 702)</option>
                  <option value="tier2">Tier 2 (Popular: 35 Jio Prefixes)</option>
                  <option value="tier3">Tier 3 (Extended: 82 All Known Jio)</option>
                </select>
              </div>

              <div>
                <label className="text-xs font-semibold uppercase tracking-wider block mb-1.5 text-slate-600 dark:text-zinc-400">
                  Max Price (USD)
                </label>
                <div className="relative">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-xs font-semibold text-slate-400">$</span>
                  <input
                    type="number"
                    step="0.05"
                    value={maxPrice}
                    onChange={(e) => setMaxPrice(e.target.value)}
                    className="field pl-7 font-mono"
                    placeholder="0.30"
                  />
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold uppercase tracking-wider block mb-1.5 text-slate-600 dark:text-zinc-400">
                  Parallel Workers
                </label>
                <input
                  type="number"
                  min="1"
                  max="5"
                  value={batchSize}
                  onChange={(e) => setBatchSize(e.target.value)}
                  className="field font-mono"
                  placeholder="2"
                />
              </div>

              <div>
                <label className="text-xs font-semibold uppercase tracking-wider block mb-1.5 text-slate-600 dark:text-zinc-400">
                  Grizzly API Key
                </label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  className="field"
                  placeholder="Keep existing key"
                />
              </div>
            </div>

            <div>
              <label className="text-xs font-semibold uppercase tracking-wider block mb-1.5 text-slate-600 dark:text-zinc-400">
                Custom Prefix Overrides (Comma Separated)
              </label>
              <input
                type="text"
                value={customPrefixes}
                onChange={(e) => setCustomPrefixes(e.target.value)}
                className="field font-mono"
                placeholder="e.g. 600, 939, 700, 701, 702"
              />
            </div>

            {/* Playwright Automation Options */}
            <div className="pt-3 border-t space-y-3" style={{ borderColor: "var(--border-color)" }}>
              <div className="flex items-center justify-between">
                <div>
                  <label className="text-xs font-semibold uppercase tracking-wider block text-slate-800 dark:text-zinc-200">
                    Playwright Auto-Verify Jio
                  </label>
                  <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                    Automatically test candidate numbers on Jio Selfcare before alerting to eliminate non-Jio zonk numbers.
                  </p>
                </div>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={verifyJio}
                    onChange={(e) => setVerifyJio(e.target.checked)}
                    className="sr-only peer"
                  />
                  <div className="w-11 h-6 bg-slate-200 peer-focus:outline-none rounded-full peer dark:bg-zinc-700 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-[#1E40AF]"></div>
                </label>
              </div>

              {verifyJio && (
                <div className="flex items-center justify-between pl-3 border-l-2 border-blue-500/40 py-1">
                  <div>
                    <label className="text-xs font-semibold uppercase tracking-wider block text-slate-800 dark:text-zinc-200">
                      Visual Browser Window (Headed Mode)
                    </label>
                    <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                      Displays the Chromium browser window on your desktop screen so you can watch Playwright type and submit in real time.
                    </p>
                  </div>
                  <label className="relative inline-flex items-center cursor-pointer">
                    <input
                      type="checkbox"
                      checked={headedBrowser}
                      onChange={(e) => setHeadedBrowser(e.target.checked)}
                      className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-slate-200 peer-focus:outline-none rounded-full peer dark:bg-zinc-700 peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-[#1E40AF]"></div>
                  </label>
                </div>
              )}
            </div>

            <div className="flex flex-col sm:flex-row items-center justify-between gap-2 pt-3 border-t" style={{ borderColor: "var(--border-color)" }}>
              <button
                type="button"
                onClick={() => sendAction("test_playwright")}
                className="w-full sm:w-auto px-3.5 py-2 rounded-lg border border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-900/50 text-xs font-semibold tactile-btn flex items-center justify-center gap-1.5"
                title="Buka Chrome Playwright sekarang untuk tes verifikasi nomor dummy"
              >
                <span>🧪</span> Test Playwright Visual Demo
              </button>

              <div className="flex items-center gap-2 w-full sm:w-auto justify-end">
                <button
                  onClick={() => setShowConfig(false)}
                  className="px-3.5 py-2 rounded-lg border text-xs font-medium tactile-btn bg-slate-100 hover:bg-slate-200 border-slate-300 text-slate-700 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300"
                >
                  Cancel
                </button>
                <button
                  onClick={saveConfig}
                  className="px-4 py-2 rounded-lg bg-[#1E40AF] hover:bg-[#1d4ed8] text-white text-xs font-semibold tactile-btn shadow-xs"
                >
                  Save Changes
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Center Stage: Hero Active Number Card */}
        <div className="panel-card space-y-6" style={{ background: "var(--hero-card-bg)" }}>
          <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 pb-4 border-b" style={{ borderColor: "var(--border-color)" }}>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-zinc-400">
                  Status:
                </span>
                {state?.is_running && !state?.is_paused && (
                  <span className="status-pill status-success">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
                    Running
                  </span>
                )}
                {state?.is_paused && (
                  <span className="status-pill status-warning">
                    Paused
                  </span>
                )}
                {!state?.is_running && (
                  <span className="status-pill status-neutral">
                    Stopped
                  </span>
                )}
              </div>
              <p className="text-xs mt-1 text-slate-500 dark:text-zinc-400 font-medium">
                {state?.status_message || "Automator initialized and ready."}
              </p>
            </div>

            {/* Tactile Action Buttons */}
            <div className="flex items-center gap-2 w-full md:w-auto">
              {!state?.is_running ? (
                <div className="flex items-center gap-2 w-full md:w-auto">
                  <button
                    onClick={() => sendAction("start")}
                    className="flex-1 md:flex-initial px-4 py-2.5 rounded-lg bg-[#1E40AF] hover:bg-[#1d4ed8] text-white font-semibold text-xs tactile-btn flex items-center justify-center gap-2 shadow-xs"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    Start Automator
                  </button>
                  {state?.active_number && (
                    <button
                      onClick={() => {
                        sendAction("clear");
                        setOtpAlert(null);
                      }}
                      className="px-3.5 py-2.5 rounded-lg border border-slate-300 dark:border-zinc-700 bg-slate-100 hover:bg-slate-200 dark:bg-zinc-800 text-slate-700 dark:text-zinc-300 text-xs font-semibold tactile-btn"
                      title="Clear active number"
                    >
                      Clear Number
                    </button>
                  )}
                </div>
              ) : (
                <>
                  {state?.is_paused ? (
                    <button
                      onClick={() => sendAction("resume")}
                      className="px-3.5 py-2 rounded-lg bg-[#1E40AF] hover:bg-[#1d4ed8] text-white font-semibold text-xs tactile-btn flex items-center gap-1.5 shadow-xs"
                    >
                      <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M8 5v14l11-7z" />
                      </svg>
                      Resume
                    </button>
                  ) : (
                    <button
                      onClick={() => sendAction("pause")}
                      className="px-3.5 py-2 rounded-lg border text-xs font-semibold tactile-btn flex items-center gap-1.5 bg-slate-100 hover:bg-slate-200 border-slate-300 text-slate-700 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300"
                    >
                      <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
                      </svg>
                      Pause
                    </button>
                  )}

                  <button
                    onClick={() => sendAction("skip")}
                    disabled={!state?.active_number}
                    className="flex-1 md:flex-initial px-4 py-2 rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/50 disabled:opacity-40 font-semibold text-xs tactile-btn flex items-center justify-center gap-1.5 shadow-xs"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
                    </svg>
                    Skip (Next Number)
                  </button>

                  <button
                    onClick={() => sendAction("stop")}
                    className="px-3.5 py-2 rounded-lg border border-rose-300 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 text-rose-800 dark:text-rose-400 hover:bg-rose-100 dark:hover:bg-rose-900/40 font-semibold text-xs tactile-btn"
                  >
                    Stop
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Large Number Hero Stage */}
          <div className="py-6 flex flex-col items-center justify-center text-center">
            {state?.active_number ? (
              <div className="space-y-4 max-w-lg w-full">
                <div className="inline-flex items-center gap-2 px-3 py-1 rounded-md bg-blue-50 text-blue-700 border border-blue-200 dark:bg-blue-950/50 dark:text-blue-300 dark:border-blue-900/50 text-xs font-semibold">
                  <span>Allocated Block:</span>
                  <span className="font-mono font-bold">Prefix {state.matched_prefix}</span>
                  {state.candidates && state.candidates.length > 0 && (
                    <span className="ml-1.5 text-slate-500 dark:text-zinc-400">
                      (+{state.candidates.length} in queue)
                    </span>
                  )}
                </div>

                <div className="space-y-1">
                  <div
                    className="text-4xl sm:text-6xl font-bold font-mono tracking-widest select-all text-slate-900 dark:text-white"
                  >
                    {state.active_number}
                  </div>
                  <div className="text-xs font-mono text-slate-500 dark:text-zinc-400">
                    International: +91 {state.active_number}
                  </div>
                </div>

                <div className="pt-2 flex flex-col sm:flex-row items-center justify-center gap-3">
                  <button
                    onClick={() => copyToClipboard(state.active_number!)}
                    className="w-full sm:w-auto px-6 py-3 rounded-lg bg-[#1E40AF] hover:bg-[#1d4ed8] text-white font-semibold text-sm tactile-btn flex items-center justify-center gap-2 shadow-sm"
                  >
                    {copied ? (
                      <>
                        <svg className="w-4 h-4 text-emerald-300" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                        </svg>
                        Copied to Clipboard
                      </>
                    ) : (
                      <>
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                        </svg>
                        Copy Mobile Number
                      </>
                    )}
                  </button>

                  <button
                    onClick={() => sendAction("skip")}
                    className="w-full sm:w-auto px-5 py-3 rounded-lg border text-xs font-semibold tactile-btn flex items-center justify-center gap-1.5 bg-slate-100 hover:bg-slate-200 border-slate-300 text-slate-800 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-200"
                  >
                    Skip (Ineligible)
                  </button>
                </div>

                <p className="text-[11px] text-slate-500 dark:text-zinc-400">
                  Paste into <b>MyJio</b> app on your device. If already used or ineligible, click <b>Skip</b>.
                </p>
              </div>
            ) : (
              <div className="py-8 text-center space-y-3">
                <div
                  className="w-12 h-12 rounded-xl border flex items-center justify-center mx-auto bg-slate-100 border-slate-200 text-slate-500 dark:bg-zinc-900 dark:border-zinc-800 dark:text-zinc-400"
                >
                  <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z" />
                  </svg>
                </div>
                <h3 className="font-semibold text-sm text-slate-800 dark:text-zinc-200">
                  {state?.is_running
                    ? state.status_message || "Searching for matching Jio prefixes..."
                    : "Automator is currently idle"}
                </h3>
                <p className="text-xs max-w-sm mx-auto text-slate-500 dark:text-zinc-400">
                  {state?.is_running
                    ? "Dispatching concurrent queries to Grizzly SMS and checking prefix tables..."
                    : "Select 'Start Automator' to begin querying virtual numbers from Grizzly SMS."}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Live Playwright Browser Capture Card */}
        <div className="panel-card space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-lg bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400 flex items-center justify-center">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              </div>
              <div>
                <p className="text-xs font-semibold uppercase tracking-wider text-slate-800 dark:text-zinc-200">
                  Live Playwright Browser View
                </p>
                <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                  Real-time visual capture of Jio Selfcare page as Playwright tests candidate numbers.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={() => setScreenshotTimestamp(Date.now())}
                className="px-2.5 py-1 text-xs rounded-md border border-slate-300 dark:border-zinc-700 text-slate-600 dark:text-zinc-300 hover:bg-slate-100 dark:hover:bg-zinc-800 tactile-btn"
                title="Refresh capture"
              >
                🔄 Refresh
              </button>
              <button
                onClick={() => setShowLiveBrowser(!showLiveBrowser)}
                className="px-3 py-1 text-xs font-semibold rounded-md bg-indigo-50 text-indigo-700 border border-indigo-200 dark:bg-indigo-950/40 dark:text-indigo-300 dark:border-indigo-800 tactile-btn"
              >
                {showLiveBrowser ? "Hide Live View" : "📸 Show Live View"}
              </button>
            </div>
          </div>

          {showLiveBrowser && (
            <div className="pt-3 border-t border-slate-200 dark:border-zinc-800 space-y-3">
              {state?.last_verified ? (
                <div className="flex flex-wrap items-center justify-between gap-2 p-2.5 rounded-lg bg-slate-50 dark:bg-zinc-900 border border-slate-200 dark:border-zinc-800 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-slate-600 dark:text-zinc-400">Verifikasi Terakhir:</span>
                    <span className="font-mono font-bold text-slate-900 dark:text-white">+91 {state.last_verified.number}</span>
                    <span className="text-slate-400 text-[11px]">(Pukul {state.last_verified.time})</span>
                  </div>
                  <div>
                    {state.last_verified.is_valid ? (
                      <span className="status-pill status-success">✅ Valid Jio Subscriber</span>
                    ) : (
                      <span className="status-pill status-warning">❌ Zonk (Bukan Nomor Jio)</span>
                    )}
                  </div>
                </div>
              ) : (
                <div className="p-3 rounded-lg bg-blue-50/70 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-900/50 text-xs text-blue-800 dark:text-blue-300 flex items-center gap-2">
                  <span className="text-base">ℹ️</span>
                  <span>
                    Playwright siap menguji halaman Jio Selfcare. Tangkapan layar otomatis diperbarui setiap kali nomor yang cocok dengan prefix diperoleh dari Grizzly SMS.
                  </span>
                </div>
              )}

              <div className="flex flex-col items-center justify-center space-y-2">
                <img
                  src={`/api/screenshot?t=${screenshotTimestamp}`}
                  alt="Jio Selfcare Live View"
                  className="rounded-lg border border-slate-300 dark:border-zinc-700 shadow-sm max-w-full h-auto max-h-[480px] object-contain"
                  onError={(e) => {
                    (e.target as HTMLElement).style.display = "none";
                  }}
                />
                <p className="text-[11px] text-slate-400">
                  Tangkapan layar aktual dari sesi Chromium Playwright.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Dashboard Panels Grid */}
        <div className="grid gap-6 md:grid-cols-2">
          {/* Refund Cooldown Queue */}
          <div className="panel-card flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border-color)" }}>
                <div className="flex items-center gap-2.5">
                  <div className="w-8 h-8 rounded-lg bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400 flex items-center justify-center">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                  </div>
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-800 dark:text-zinc-200">
                      Cancellation Refund Queue
                    </p>
                    <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                      Enforced 120s cooldown before API allows refund.
                    </p>
                  </div>
                </div>
                <span className="text-xs font-mono text-slate-500 dark:text-zinc-400 font-medium">
                  {state?.cooldown_pending?.length || 0} pending
                </span>
              </div>

              <div className="space-y-2 max-h-48 overflow-y-auto pr-1">
                {state?.cooldown_pending && state.cooldown_pending.length > 0 ? (
                  state.cooldown_pending.map((item) => (
                    <div
                      key={item.activation_id}
                      className="p-2.5 rounded-lg border flex items-center justify-between text-xs transition-colors bg-slate-50 border-slate-200 dark:bg-zinc-900/60 dark:border-zinc-800"
                    >
                      <div className="font-mono text-slate-800 dark:text-zinc-200">
                        +91 {item.phone_number}
                        <span className="ml-1.5 text-slate-500 dark:text-zinc-400">(#{item.activation_id})</span>
                      </div>
                      <div>
                        {item.remaining_cooldown > 0 ? (
                          <span className="inline-flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400 font-medium">
                            <span className="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
                            Refund in {item.remaining_cooldown}s
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400 font-medium">
                            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
                            Refunding now
                          </span>
                        )}
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="py-7 text-center text-xs text-slate-500 dark:text-zinc-400">
                    No numbers currently awaiting cancellation cooldown.
                  </div>
                )}
              </div>
            </div>

            <p className="text-[11px] pt-3 border-t mt-3 text-slate-500 dark:text-zinc-400" style={{ borderColor: "var(--border-color)" }}>
              Numbers discarded during search are queued here and automatically cancelled once 120s passes to guarantee balance preservation.
            </p>
          </div>

          {/* Session Metrics KPI Cards */}
          <div className="panel-card flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border-color)" }}>
                <div className="flex items-center gap-2.5">
                  <div className="w-8 h-8 rounded-lg bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400 flex items-center justify-center">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                    </svg>
                  </div>
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-wider text-slate-800 dark:text-zinc-200">
                      Performance Telemetry
                    </p>
                    <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                      Real-time activation statistics and queue capacity.
                    </p>
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div className="p-3.5 rounded-lg border text-center bg-slate-50 border-slate-200 dark:bg-zinc-900/60 dark:border-zinc-800">
                  <span className="text-[11px] uppercase tracking-wider block text-slate-500 dark:text-zinc-400 font-medium">
                    OTPs Received
                  </span>
                  <span className="text-2xl font-bold text-emerald-700 dark:text-emerald-400 font-mono">
                    {state?.otps_count || 0}
                  </span>
                </div>

                <div className="p-3.5 rounded-lg border text-center bg-slate-50 border-slate-200 dark:bg-zinc-900/60 dark:border-zinc-800">
                  <span className="text-[11px] uppercase tracking-wider block text-slate-500 dark:text-zinc-400 font-medium">
                    Total Attempts
                  </span>
                  <span className="text-2xl font-bold font-mono text-blue-700 dark:text-blue-400">
                    {state?.searches_count || 0}
                  </span>
                </div>

                <div className="p-3.5 rounded-lg border text-center bg-slate-50 border-slate-200 dark:bg-zinc-900/60 dark:border-zinc-800">
                  <span className="text-[11px] uppercase tracking-wider block text-slate-500 dark:text-zinc-400 font-medium">
                    Queue Buffer
                  </span>
                  <span className="text-2xl font-bold text-amber-700 dark:text-amber-400 font-mono">
                    {state?.candidates?.length || 0}
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-4 pt-3 border-t flex items-center justify-between text-xs" style={{ borderColor: "var(--border-color)", color: "var(--text-secondary)" }}>
              <span>Preset Mode: <b className="font-semibold text-slate-800 dark:text-zinc-100">{state?.config?.preset}</b></span>
              <span>Price Ceiling: <b className="font-semibold text-slate-800 dark:text-zinc-100">${state?.config?.max_price}</b></span>
            </div>
          </div>
        </div>

        {/* Live Terminal Console (Developer Stream) */}
        <div
          className="panel-card p-0 overflow-hidden border border-slate-300 dark:border-zinc-800 bg-white dark:bg-[#09090b] shadow-xs"
        >
          <div className="px-4 py-2.5 bg-slate-100 dark:bg-zinc-950 border-b border-slate-200 dark:border-zinc-800 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-slate-400 dark:bg-zinc-700"></span>
              <span className="w-2.5 h-2.5 rounded-full bg-slate-400 dark:bg-zinc-700"></span>
              <span className="w-2.5 h-2.5 rounded-full bg-slate-400 dark:bg-zinc-700"></span>
              <span className="text-xs font-mono font-medium text-slate-700 dark:text-zinc-400 ml-2">Console Stream (stdout)</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setAutoScrollLogs(!autoScrollLogs)}
                className={`text-[10px] font-mono font-medium tactile-btn px-2.5 py-1 rounded border transition-colors ${
                  autoScrollLogs
                    ? "border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-400"
                    : "border-slate-300 bg-slate-200/70 text-slate-600 hover:bg-slate-200 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-500 dark:hover:bg-zinc-800"
                }`}
                title="Toggle internal log auto-scrolling"
              >
                Auto-scroll: {autoScrollLogs ? "ON" : "OFF"}
              </button>
              <button
                onClick={() => setLogs([])}
                className="text-[11px] text-slate-500 hover:text-slate-800 dark:text-zinc-500 dark:hover:text-zinc-300 font-mono tactile-btn px-1.5 py-0.5"
              >
                Clear Buffer
              </button>
            </div>
          </div>

          <div
            ref={terminalContainerRef}
            className="p-4 font-mono text-xs max-h-60 overflow-y-auto terminal-scroll space-y-1 bg-slate-50 dark:bg-[#09090b] text-slate-800 dark:text-zinc-300 selection:bg-blue-100 dark:selection:bg-zinc-800"
          >
            {logs.length === 0 ? (
              <div className="text-slate-400 dark:text-zinc-600 py-2">No events logged yet. Connect to automator to view activity.</div>
            ) : (
              logs.map((log, i) => {
                let colorClass = "text-slate-700 dark:text-zinc-300";
                if (log.level === "SUCCESS") colorClass = "text-emerald-700 dark:text-emerald-400 font-semibold";
                if (log.level === "WARN") colorClass = "text-amber-700 dark:text-amber-400 font-medium";
                if (log.level === "ERROR") colorClass = "text-rose-700 dark:text-rose-400 font-bold";
                if (log.level === "CLEANUP") colorClass = "text-blue-700 dark:text-blue-400 font-medium";

                return (
                  <div key={i} className="leading-relaxed">
                    <span className="text-slate-400 dark:text-zinc-600 select-none mr-2 font-mono">[{log.timestamp}]</span>
                    <span className={colorClass}>{log.message}</span>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Mitra UI Data Table: Recent Activations */}
        {history.length > 0 && (
          <div className="panel-card space-y-3">
            <div className="flex items-center justify-between pb-3 border-b" style={{ borderColor: "var(--border-color)" }}>
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-400 flex items-center justify-center">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
                  </svg>
                </div>
                <div>
                  <p className="text-xs font-semibold uppercase tracking-wider text-slate-800 dark:text-zinc-200">
                    Activation Event Log
                  </p>
                  <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                    Historical record of acquired virtual numbers and OTP codes.
                  </p>
                </div>
              </div>
              <span className="text-xs font-mono text-slate-500 dark:text-zinc-400">
                {history.length} records
              </span>
            </div>

            <div className="table-wrap rounded-lg border border-slate-200 dark:border-zinc-800 overflow-x-auto bg-white dark:bg-transparent">
              <table className="w-full text-left text-xs">
                <thead>
                  <tr className="bg-slate-50 text-slate-700 border-b border-slate-200 dark:bg-zinc-900/80 dark:text-zinc-400 dark:border-zinc-800">
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px]">Timestamp</th>
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px]">National Number</th>
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px]">Prefix Block</th>
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px]">Outcome</th>
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px]">Verification OTP</th>
                    <th className="py-2.5 px-3 font-semibold uppercase tracking-wide text-[11px] text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y font-mono">
                  {history.map((item, idx) => (
                    <tr key={idx} className="border-b border-slate-200 dark:border-zinc-800/80 hover:bg-slate-50 dark:hover:bg-zinc-900/40 transition-colors">
                      <td className="py-2.5 px-3 text-slate-500 dark:text-zinc-400">{item.time}</td>
                      <td className="py-2.5 px-3 font-bold text-slate-900 dark:text-zinc-100">+91 {item.phone}</td>
                      <td className="py-2.5 px-3 text-blue-700 dark:text-blue-400 font-semibold">{item.prefix}</td>
                      <td className="py-2.5 px-3">
                        {item.status === "SUCCESS" ? (
                          <span className="status-pill status-success">Success</span>
                        ) : (
                          <span className="status-pill status-warning">Skipped</span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-emerald-700 dark:text-emerald-400 font-bold">
                        {item.otp || "—"}
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <button
                          onClick={() => copyToClipboard(item.phone)}
                          className="px-2 py-1 rounded tactile-btn border text-[11px] bg-slate-100 hover:bg-slate-200 border-slate-300 text-slate-700 dark:bg-zinc-800 dark:border-zinc-700 dark:text-zinc-300"
                          title="Copy phone"
                        >
                          Copy
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t py-3 text-center text-xs text-slate-500 dark:text-zinc-500" style={{ borderColor: "var(--border-color)" }}>
        Grizzly SMS MyJio Bot • Standardized with Mitra UI Design System
      </footer>
    </div>
  );
}
