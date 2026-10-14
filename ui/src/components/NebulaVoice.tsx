import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Code2, Info, Play, Radio, RefreshCw, Target, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { VoiceWorkDetail, VoiceWorkRoster } from "../api/types";
import styles from "./NebulaVoice.module.css";

type Phase = "connecting" | "listening" | "speaking" | "reconnecting" | "error" | "closing";
type Fragment = { delta: string; start_ms: number; end_ms: number };
type VoiceEvent = {
  type?: string;
  delta?: string;
  start_ms?: number;
  end_ms?: number;
  offset_ms?: number;
  delegation?: { id?: string; target?: string };
  error?: { message?: string; code?: string };
};
type Connection = { peer: RTCPeerConnection; channel: RTCDataChannel; microphone: MediaStream; sessionId?: string };

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
const spokenResult = (text: string) => text.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/[`*_#]/g, "").slice(0, 400);

export function MicrophoneIcon({ muted = false, size = 19 }: { muted?: boolean; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10a7 7 0 0 0 14 0M12 17v5m-4 0h8" />{muted && <path d="M3 3l18 18" strokeWidth="2.5" />}</svg>;
}

async function waitForIce(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === "complete") return;
  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => { peer.removeEventListener("icegatheringstatechange", onChange); reject(new Error("Voice connection timed out. Try again.")); }, 10_000);
    const onChange = () => {
      if (peer.iceGatheringState !== "complete") return;
      window.clearTimeout(timeout);
      peer.removeEventListener("icegatheringstatechange", onChange);
      resolve();
    };
    peer.addEventListener("icegatheringstatechange", onChange);
    onChange();
  });
}

export function NebulaVoice({ api, project, initialMicrophone, onDelegate, onExit }: {
  api: ApiClient;
  project: { id: string; name: string };
  initialMicrophone?: Promise<MediaStream>;
  onDelegate: (request: string) => Promise<string | undefined>;
  onExit: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("connecting");
  const [error, setError] = useState("");
  const [microphoneMuted, setMicrophoneMuted] = useState(false);
  const [playbackBlocked, setPlaybackBlocked] = useState(false);
  const [callerCaption, setCallerCaption] = useState("");
  const [assistantCaption, setAssistantCaption] = useState("");
  const [work, setWork] = useState<VoiceWorkRoster>();
  const [workError, setWorkError] = useState("");
  const [selected, setSelected] = useState<{ kind: "chats" | "missions"; id: string }>();
  const [detail, setDetail] = useState<VoiceWorkDetail>();
  const [detailError, setDetailError] = useState("");
  const [delegatedStatus, setDelegatedStatus] = useState("");
  const audio = useRef<HTMLAudioElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const exitButton = useRef<HTMLButtonElement>(null);
  const connection = useRef<Connection | undefined>(undefined);
  const generation = useRef(0);
  const mounted = useRef(true);
  const closing = useRef(false);
  const fragments = useRef<Fragment[]>([]);
  const lastInputAt = useRef(0);
  const delegatedThrough = useRef(0);
  const delegationIds = useRef(new Set<string>());
  const taskQueue = useRef(Promise.resolve());
  const projectAtConnection = useRef(project.id);

  const refreshWork = useCallback(async () => {
    try {
      const next = await api.getVoiceWorkRoster(project.id);
      if (!mounted.current) return;
      setWork(next);
      setWorkError("");
    } catch {
      if (mounted.current) setWorkError("Work status is unavailable. Retry to refresh it.");
    }
  }, [api, project.id]);

  const release = useCallback(() => {
    const active = connection.current;
    connection.current = undefined;
    if (!active) return;
    active.microphone.getTracks().forEach((track) => track.stop());
    active.channel.close();
    active.peer.close();
    if (audio.current) audio.current.srcObject = null;
  }, []);

  const sendEvent = useCallback((event: Record<string, unknown>) => {
    const channel = connection.current?.channel;
    if (channel?.readyState === "open") channel.send(JSON.stringify(event));
  }, []);

  const delegate = useCallback(async (id: string, offsetMs: number, attempt: number) => {
    // Live delegation carries an ID and time, not the user's words. Allow late
    // transcript fragments to arrive, then use only new speech for this request.
    const started = Date.now();
    while (Date.now() - started < 1800) {
      const heard = fragments.current.some((part) => part.end_ms > delegatedThrough.current && part.start_ms <= offsetMs + 700);
      if (heard && Date.now() - lastInputAt.current >= 450) break;
      await sleep(100);
    }
    if (!mounted.current || closing.current || attempt !== generation.current || projectAtConnection.current !== project.id) return;
    const chunks = fragments.current.filter((part) => part.end_ms > delegatedThrough.current && part.start_ms <= offsetMs + 700);
    const request = chunks.map((part) => part.delta).join("").trim();
    if (chunks.length) delegatedThrough.current = Math.max(delegatedThrough.current, ...chunks.map((part) => part.end_ms));
    if (!request) {
      sendEvent({ type: "session.commentary.append", event_id: `voice_unclear_${crypto.randomUUID()}`, delegation_id: id, content: "I didn't catch enough of that request. Please say it again." });
      return;
    }
    setDelegatedStatus(`Checking: ${request.slice(0, 110)}`);
    try {
      const result = await onDelegate(request);
      if (!mounted.current || closing.current || attempt !== generation.current || projectAtConnection.current !== project.id) return;
      sendEvent({
        type: "session.commentary.append", event_id: `voice_result_${crypto.randomUUID()}`,
        delegation_id: id,
        content: spokenResult(result?.trim() || "Nebula could not confirm that request. Please check the work status and try again."),
      });
      setDelegatedStatus(result ? "Nebula responded" : "Needs retry");
      void refreshWork();
    } catch (caught) {
      if (!mounted.current || closing.current || attempt !== generation.current) return;
      const message = caught instanceof Error ? caught.message : "The request failed.";
      sendEvent({ type: "session.commentary.append", event_id: `voice_failed_${crypto.randomUUID()}`, delegation_id: id,
        content: `Nebula could not finish the request: ${message.slice(0, 240)}. Please retry or check the work status.` });
      setDelegatedStatus("Request failed. Try again.");
    }
  }, [onDelegate, project.id, refreshWork, sendEvent]);

  const handleEvent = useCallback((event: VoiceEvent) => {
    if (event.type === "session.started") {
      setPhase("listening");
      setError("");
    } else if (event.type === "session.closed") {
      const didExit = closing.current;
      closing.current = false;
      release();
      if (didExit) onExit();
      else { setPhase("error"); setError("The voice connection ended. Select Reconnect to continue."); }
    } else if (event.type === "session.input_transcript.delta" && typeof event.delta === "string") {
      const start = event.start_ms ?? 0;
      const end = event.end_ms ?? start;
      fragments.current.push({ delta: event.delta, start_ms: start, end_ms: end });
      fragments.current = fragments.current.slice(-100);
      lastInputAt.current = Date.now();
      setCallerCaption((current) => (current + event.delta).slice(-480));
      setPhase("listening");
    } else if (event.type === "session.output_transcript.delta" && typeof event.delta === "string") {
      setAssistantCaption((current) => (current + event.delta).slice(-480));
      setPhase("speaking");
    } else if (event.type === "session.delegation.created" && event.delegation?.target === "client" && event.delegation.id) {
      const id = event.delegation.id;
      if (delegationIds.current.has(id)) return;
      delegationIds.current.add(id);
      const attempt = generation.current;
      taskQueue.current = taskQueue.current.then(() => delegate(id, event.offset_ms ?? Number.MAX_SAFE_INTEGER, attempt));
    } else if (event.type === "error") {
      setError(event.error?.message || "The voice service reported an error. Try reconnecting if it persists.");
    }
  }, [delegate, onExit, release]);

  const connect = useCallback(async (initial?: Promise<MediaStream>) => {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setPhase("error");
      setError("Microphone access needs a trusted HTTPS address on this device, or localhost on the Nebula host. Open Nebula through HTTPS and retry.");
      return;
    }
    const attempt = ++generation.current;
    closing.current = false;
    release();
    fragments.current = [];
    lastInputAt.current = 0;
    delegatedThrough.current = 0;
    delegationIds.current.clear();
    setPhase("connecting");
    setError("");
    setPlaybackBlocked(false);
    setMicrophoneMuted(false);
    setCallerCaption("");
    setAssistantCaption("");
    try {
      const microphone = await (initial ?? navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }));
      if (!mounted.current || attempt !== generation.current) { microphone.getTracks().forEach((track) => track.stop()); return; }
      const peer = new RTCPeerConnection();
      const channel = peer.createDataChannel("oai-events");
      const current: Connection = { peer, channel, microphone };
      connection.current = current;
      microphone.getAudioTracks().forEach((track) => peer.addTrack(track, microphone));
      peer.addEventListener("track", (event) => {
        if (!audio.current || attempt !== generation.current) return;
        audio.current.srcObject = event.streams[0] ?? new MediaStream([event.track]);
        void audio.current.play().catch(() => setPlaybackBlocked(true));
      });
      peer.addEventListener("connectionstatechange", () => {
        if (connection.current !== current || attempt !== generation.current || closing.current) return;
        if (peer.connectionState === "failed") { setPhase("error"); setError("The voice connection failed. Select Reconnect to continue."); }
        else if (peer.connectionState === "disconnected") setPhase("reconnecting");
        else if (peer.connectionState === "connected") setPhase((currentPhase) => currentPhase === "reconnecting" ? "listening" : currentPhase);
      });
      channel.addEventListener("message", ({ data }) => {
        if (attempt !== generation.current) return;
        try { handleEvent(JSON.parse(String(data)) as VoiceEvent); }
        catch { setError("A voice event could not be read. Reconnect if conversation stops."); }
      });
      channel.addEventListener("close", () => {
        if (connection.current !== current || attempt !== generation.current || closing.current) return;
        setPhase("error");
        setError("The voice event channel closed. Select Reconnect to continue.");
      });
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      await waitForIce(peer);
      if (attempt !== generation.current) return;
      const sdp = peer.localDescription?.sdp;
      if (!sdp) throw new Error("The browser could not create a voice offer. Try again.");
      const result = await api.startVoiceSession(project.id, sdp);
      if (attempt !== generation.current) return;
      current.sessionId = result.session_id;
      projectAtConnection.current = project.id;
      await peer.setRemoteDescription({ type: "answer", sdp: result.sdp });
    } catch (caught) {
      if (attempt !== generation.current || !mounted.current) return;
      release();
      setPhase("error");
      if (caught instanceof DOMException && caught.name === "NotAllowedError") setError("Microphone access was denied. Allow the microphone for this site in your browser, then select Reconnect.");
      else if (caught instanceof DOMException && caught.name === "NotFoundError") setError("No microphone was found on this device. Connect one and select Reconnect.");
      else setError(caught instanceof Error ? caught.message : "Voice could not start. Select Reconnect to try again.");
    }
  }, [api, handleEvent, project.id, release]);

  useEffect(() => {
    mounted.current = true;
    exitButton.current?.focus();
    void connect(initialMicrophone);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      mounted.current = false;
      ++generation.current;
      document.body.style.overflow = oldOverflow;
      release();
    };
    // Voice mode is one session per selected project; a changed project is
    // handled by the explicit project effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void refreshWork();
    const interval = window.setInterval(() => void refreshWork(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshWork]);

  useEffect(() => {
    if (projectAtConnection.current === project.id) return;
    ++generation.current;
    release();
    setPhase("error");
    setError(`The selected project changed to ${project.name}. Select Reconnect to use voice in this project.`);
    setWork(undefined);
    setSelected(undefined);
    void refreshWork();
  }, [project.id, project.name, refreshWork, release]);

  useEffect(() => {
    if (!selected) { setDetail(undefined); return; }
    const controller = new AbortController();
    setDetail(undefined);
    setDetailError("");
    void api.getVoiceWorkDetail(project.id, selected.kind, selected.id, controller.signal)
      .then(setDetail)
      .catch(() => { if (!controller.signal.aborted) setDetailError("This work item could not be loaded. Refresh its status and try again."); });
    return () => controller.abort();
  }, [api, project.id, selected]);

  const exit = () => {
    if (closing.current) return;
    closing.current = true;
    setPhase("closing");
    const channel = connection.current?.channel;
    if (channel?.readyState === "open") {
      channel.send(JSON.stringify({ type: "session.close", event_id: `voice_close_${crypto.randomUUID()}` }));
      window.setTimeout(() => { if (closing.current && mounted.current) { closing.current = false; release(); onExit(); } }, 5000);
    } else { closing.current = false; release(); onExit(); }
  };

  const toggleMicrophone = () => {
    const next = !microphoneMuted;
    connection.current?.microphone.getAudioTracks().forEach((track) => { track.enabled = !next; });
    setMicrophoneMuted(next);
  };

  const phaseLabel = phase === "connecting" ? "Connecting" : phase === "speaking" ? "Speaking" : phase === "listening" ? "Listening" : phase === "reconnecting" ? "Reconnecting" : phase === "closing" ? "Ending voice" : "Connection needs attention";

  return <div ref={screen} className={styles.screen} role="dialog" aria-modal="true" aria-label="Nebula Voice" onKeyDown={(event) => {
    if (event.key === "Escape") { event.preventDefault(); exit(); }
    if (event.key !== "Tab") return;
    const buttons = [...(screen.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    if (!buttons.length) return;
    const first = buttons[0], last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }}>
    <audio ref={audio} autoPlay playsInline className={styles.audio} />
    <div className={styles.header}>
      <div className={styles.identity}><strong>Nebula <span>Voice</span></strong><small>{project.name}</small></div>
      <button ref={exitButton} className={styles.exit} type="button" title="Exit voice mode" aria-label="Exit voice mode" onClick={exit} disabled={phase === "closing"}><X size={20} aria-hidden="true" /><span>Exit voice mode</span></button>
    </div>
    <div className={styles.layout}>
      <section className={styles.center} aria-label="Voice conversation">
        <div data-testid="voice-orb" className={`${styles.orb} ${phase === "speaking" ? styles.speaking : ""}`} aria-hidden="true"><div /></div>
        <p className={styles.phase} role="status">{microphoneMuted && phase !== "error" ? "Microphone off" : phaseLabel}</p>
        <div className={styles.controls}>
          <button type="button" onClick={toggleMicrophone} disabled={!connection.current || phase === "closing"} title={microphoneMuted ? "Turn microphone on" : "Turn microphone off"} aria-label={microphoneMuted ? "Turn microphone on" : "Turn microphone off"}><MicrophoneIcon muted={microphoneMuted} /></button>
          {(phase === "error" || phase === "reconnecting") && <button type="button" onClick={() => void connect()} title="Reconnect voice" aria-label="Reconnect voice"><RefreshCw size={19} /><span>Reconnect</span></button>}
          {playbackBlocked && <button type="button" onClick={() => { void audio.current?.play().then(() => setPlaybackBlocked(false)).catch(() => setError("Audio playback is blocked. Allow sound for this site and retry.")); }}><Play size={17} /> Play audio</button>}
        </div>
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.captions} aria-live="polite"><p><small>You</small>{callerCaption || "Speak to ask about this project or direct its work."}</p><p><small>Nebula</small>{assistantCaption || "Ready for a conversation."}</p></div>
        {delegatedStatus && <p className={styles.delegated} role="status">{delegatedStatus}</p>}
      </section>
      <aside className={styles.work} aria-label="Project work">
        <div className={styles.workHeader}><h2>Project work</h2><button type="button" title="Refresh work status" aria-label="Refresh work status" onClick={() => void refreshWork()}><RefreshCw size={17} /></button></div>
        {workError && <p role="alert" className={styles.error}>{workError}</p>}
        {!work && !workError && <p className={styles.empty}>Loading work…</p>}
        {work && !work.chats.length && !work.missions.length && <p className={styles.empty}>No chats or missions yet. Ask Nebula to start one.</p>}
        {work?.chats.slice(0, 4).map((chat) => <button key={chat.id} type="button" className={styles.workCard} aria-expanded={selected?.kind === "chats" && selected.id === chat.id} onClick={() => setSelected((current) => current?.kind === "chats" && current.id === chat.id ? undefined : { kind: "chats", id: chat.id })}><Code2 size={20} aria-hidden="true" /><span><strong>{chat.title}</strong><small>{chat.state === "working" ? "Working" : "Ready"} · {chat.model}</small></span>{selected?.kind === "chats" && selected.id === chat.id ? <ChevronUp size={17} /> : <ChevronDown size={17} />}</button>)}
        {work?.missions.slice(0, 4).map((mission) => <button key={mission.id} type="button" className={styles.workCard} aria-expanded={selected?.kind === "missions" && selected.id === mission.id} onClick={() => setSelected((current) => current?.kind === "missions" && current.id === mission.id ? undefined : { kind: "missions", id: mission.id })}><Target size={20} aria-hidden="true" /><span><strong>{mission.name}</strong><small>{mission.status.replaceAll("_", " ")}</small></span>{selected?.kind === "missions" && selected.id === mission.id ? <ChevronUp size={17} /> : <ChevronDown size={17} />}</button>)}
        {(work?.more_chats || work?.more_missions) && <p className={styles.empty}>Showing recent work. Ask Nebula for older items.</p>}
        {selected && <div className={styles.detail} role="region" aria-label="Selected work details">
          {detailError && <p role="alert">{detailError}</p>}
          {!detail && !detailError && <p>Loading details…</p>}
          {detail && <><strong>{detail.title ?? detail.name}</strong><small>{detail.status ?? detail.pending_turn?.status ?? "Ready"}</small>{detail.objective && <p>{detail.objective}</p>}{detail.recent_messages?.slice(-3).map((message, index) => <p key={index}><b>{message.role === "user" ? "You" : "Assistant"}:</b> {message.content}</p>)}{detail.tasks?.slice(0, 5).map((task) => <p key={task.id}>{task.title} · {task.status}</p>)}</>}
        </div>}
      </aside>
    </div>
    <footer className={styles.footer}><Radio size={16} aria-hidden="true" /><span>Keep talking. Voice stays on until you exit.</span><Info size={15} aria-hidden="true" /></footer>
  </div>;
}
