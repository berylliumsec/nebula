import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Code2, Info, Play, Radio, RefreshCw, Target, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { VoiceWorkDetail, VoiceWorkRoster } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { selectDelegatedVoiceTurns, type VoiceTranscriptPart } from "./voiceTranscript";
import styles from "./NebulaVoice.module.css";

type Phase = "connecting" | "listening" | "speaking" | "reconnecting" | "error" | "closing" | "paused";
type CloseReason = "idle" | "muted" | "background" | "reconnect" | "exit";
type VoiceEvent = {
  type?: string;
  reason?: string;
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
const captionPreview = (text: string) => text.length > 100 ? `…${text.slice(-72).trimStart()}` : text;

export function MicrophoneIcon({ muted = false, size = 19 }: { muted?: boolean; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10a7 7 0 0 0 14 0M12 17v5m-4 0h8" />{muted && <path d="M3 3l18 18" strokeWidth="2.5" />}</svg>;
}

function SpeakerIcon() {
  return <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5ZM15 9a4 4 0 0 1 0 6M18 6a8 8 0 0 1 0 12" /></svg>;
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
  const [microphonePanelOpen, setMicrophonePanelOpen] = useState(false);
  const [microphoneMessage, setMicrophoneMessage] = useState("");
  const [microphoneDevices, setMicrophoneDevices] = useState<Array<{ id: string; label: string }>>([]);
  const [microphoneDeviceId, setMicrophoneDeviceId] = useState("default");
  const [microphoneLabel, setMicrophoneLabel] = useState("");
  const [speakerPanelOpen, setSpeakerPanelOpen] = useState(false);
  const [speakerMessage, setSpeakerMessage] = useState("");
  const [speakerDevices, setSpeakerDevices] = useState<Array<{ id: string; label: string }>>([]);
  const [speakerDeviceId, setSpeakerDeviceId] = useState("default");
  const [callerCaption, setCallerCaption] = useState("");
  const [assistantCaption, setAssistantCaption] = useState("");
  const [callerExpanded, setCallerExpanded] = useState(false);
  const [assistantExpanded, setAssistantExpanded] = useState(false);
  const inputPanelId = useId();
  const speakerPanelId = useId();
  const callerCaptionId = useId();
  const assistantCaptionId = useId();
  const [work, setWork] = useState<VoiceWorkRoster>();
  const [workError, setWorkError] = useState("");
  const [selected, setSelected] = useState<{ kind: "chats" | "missions"; id: string }>();
  const [detail, setDetail] = useState<VoiceWorkDetail>();
  const [detailError, setDetailError] = useState("");
  const [delegatedStatus, setDelegatedStatus] = useState("");
  const [pauseMessage, setPauseMessage] = useState("");
  const audio = useRef<HTMLAudioElement>(null);
  const orb = useRef<HTMLDivElement>(null);
  const inputToggle = useRef<HTMLButtonElement>(null);
  const speakerToggle = useRef<HTMLButtonElement>(null);
  const audioMeter = useRef<{ context: AudioContext; source: MediaStreamAudioSourceNode; analyser: AnalyserNode; silence: GainNode; frame: number } | undefined>(undefined);
  const outputMeter = useRef<{ context: AudioContext; source: MediaStreamAudioSourceNode; analyser: AnalyserNode; silence: GainNode; frame: number } | undefined>(undefined);
  const screen = useRef<HTMLDivElement>(null);
  const exitButton = useRef<HTMLButtonElement>(null);
  const connection = useRef<Connection | undefined>(undefined);
  const remoteTrack = useRef<MediaStreamTrack | undefined>(undefined);
  const generation = useRef(0);
  const mounted = useRef(true);
  const closing = useRef(false);
  const closeReason = useRef<CloseReason | undefined>(undefined);
  const closeTimer = useRef<number | undefined>(undefined);
  const pauseSeconds = useRef(20);
  const lastActivityAt = useRef(Date.now());
  const lastPlaybackAt = useRef(0);
  const playbackStats = useRef<{ connection: Connection; energy: number } | undefined>(undefined);
  const statsInFlight = useRef(false);
  const mutePending = useRef(false);
  const fragments = useRef<VoiceTranscriptPart[]>([]);
  const transcript = useRef<VoiceTranscriptPart[]>([]);
  const transcriptSequence = useRef(0);
  const captionPosition = useRef<{ speaker: VoiceTranscriptPart["speaker"]; endMs: number } | undefined>(undefined);
  const lastInputAt = useRef(0);
  const lastInputSequence = useRef(0);
  const delegatedThrough = useRef(0);
  const lastCoreResult = useRef<{ endMs: number; text: string } | undefined>(undefined);
  const unroutedTimer = useRef<number | undefined>(undefined);
  const delegationIds = useRef(new Set<string>());
  const taskQueue = useRef(Promise.resolve());
  const projectAtConnection = useRef(project.id);
  const preferredMicrophone = useRef<string | undefined>(undefined);

  const refreshMicrophones = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (!mounted.current) return;
      setMicrophoneDevices(devices.filter((device) => device.kind === "audioinput" && device.deviceId !== "default")
        .map((device, index) => ({ id: device.deviceId, label: device.label || `Microphone ${index + 1}` })));
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.microphones_unavailable", "Microphone devices could not be listed.", caught, "voice-input");
      if (mounted.current) setMicrophoneMessage("This browser could not list microphones. Check this site's microphone permission.");
    }
  }, []);

  const refreshWork = useCallback(async () => {
    try {
      const next = await api.getVoiceWorkRoster(project.id);
      if (!mounted.current) return;
      setWork(next);
      setWorkError("");
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.work_roster_failed", "Project work could not be refreshed in Voice.", caught, "voice-work");
      if (mounted.current) setWorkError("Work status is unavailable. Retry to refresh it.");
    }
  }, [api, project.id]);

  const release = useCallback(() => {
    window.clearTimeout(closeTimer.current);
    closeTimer.current = undefined;
    const active = connection.current;
    connection.current = undefined;
    playbackStats.current = undefined;
    remoteTrack.current = undefined;
    const meter = audioMeter.current;
    audioMeter.current = undefined;
    if (meter) {
      window.cancelAnimationFrame(meter.frame);
      meter.source.disconnect();
      meter.analyser.disconnect();
      meter.silence.disconnect();
      void meter.context.close().catch((caught) => {
        logCaughtDiagnostic("interface.voice.meter_close_failed", "The microphone level meter could not close cleanly.", caught, "voice-input");
      });
    }
    const playback = outputMeter.current;
    outputMeter.current = undefined;
    if (playback) {
      window.cancelAnimationFrame(playback.frame);
      playback.source.disconnect();
      playback.analyser.disconnect();
      playback.silence.disconnect();
      void playback.context.close().catch((caught) => {
        logCaughtDiagnostic("interface.voice.output_meter_close_failed", "The speaker activity meter could not close cleanly.", caught, "voice-output");
      });
    }
    orb.current?.style.setProperty("--voice-scale", "1");
    orb.current?.style.setProperty("--voice-rise", "0px");
    if (!active) return;
    active.microphone.getTracks().forEach((track) => track.stop());
    active.channel.close();
    active.peer.close();
    if (audio.current) audio.current.srcObject = null;
  }, []);

  const startMicrophoneMeter = useCallback((microphone: MediaStream) => {
    if (!window.AudioContext) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    let created: AudioContext | undefined;
    try {
      const context = created = new AudioContext();
      const source = context.createMediaStreamSource(microphone);
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      const silence = context.createGain();
      silence.gain.value = 0;
      source.connect(analyser);
      analyser.connect(silence);
      silence.connect(context.destination);
      const samples = new Uint8Array(analyser.fftSize);
      let level = 0;
      const meter = { context, source, analyser, silence, frame: 0 };
      audioMeter.current = meter;
      const tick = () => {
        if (audioMeter.current !== meter) return;
        analyser.getByteTimeDomainData(samples);
        const energy = samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0) / samples.length;
        const inputEnabled = microphone.getAudioTracks().some((track) => track.enabled);
        const amplitude = Math.sqrt(energy);
        if (inputEnabled && amplitude > 0.045) lastActivityAt.current = Date.now();
        const target = inputEnabled ? Math.min(1, Math.max(0, (amplitude - 0.018) * 9)) : 0;
        level = level * 0.64 + target * 0.36;
        if (!reducedMotion) {
          orb.current?.style.setProperty("--voice-scale", (1 + level * 0.11).toFixed(3));
          orb.current?.style.setProperty("--voice-rise", `${(-level * 8).toFixed(1)}px`);
        }
        meter.frame = window.requestAnimationFrame(tick);
      };
      void context.resume().catch((caught) => {
        logCaughtDiagnostic("interface.voice.meter_resume_failed", "The microphone level meter could not start.", caught, "voice-input");
        if (mounted.current) setMicrophoneMessage("Voice is connected, but microphone animation is unavailable on this device.");
      });
      meter.frame = window.requestAnimationFrame(tick);
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.meter_setup_failed", "The microphone level meter could not be created.", caught, "voice-input");
      // Voice remains usable if a browser cannot expose microphone levels.
      void created?.close().catch((closeError) => {
        logCaughtDiagnostic("interface.voice.meter_close_failed", "The microphone level meter could not close cleanly.", closeError, "voice-input");
      });
    }
  }, []);

  const startOutputMeter = useCallback((stream: MediaStream) => {
    if (!window.AudioContext) return;
    let created: AudioContext | undefined;
    try {
      const context = created = new AudioContext();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      const silence = context.createGain();
      silence.gain.value = 0;
      source.connect(analyser);
      analyser.connect(silence);
      silence.connect(context.destination);
      const samples = new Uint8Array(analyser.fftSize);
      const meter = { context, source, analyser, silence, frame: 0 };
      outputMeter.current = meter;
      const tick = () => {
        if (outputMeter.current !== meter) return;
        analyser.getByteTimeDomainData(samples);
        const energy = samples.reduce((sum, sample) => sum + ((sample - 128) / 128) ** 2, 0) / samples.length;
        if (Math.sqrt(energy) > 0.012) {
          lastPlaybackAt.current = Date.now();
          lastActivityAt.current = lastPlaybackAt.current;
        }
        meter.frame = window.requestAnimationFrame(tick);
      };
      void context.resume().catch((caught) => {
        logCaughtDiagnostic("interface.voice.output_meter_resume_failed", "The speaker activity meter could not start.", caught, "voice-output");
      });
      meter.frame = window.requestAnimationFrame(tick);
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.output_meter_setup_failed", "The speaker activity meter could not be created.", caught, "voice-output");
      void created?.close().catch(() => undefined);
    }
  }, []);

  const sendEvent = useCallback((event: Record<string, unknown>) => {
    const channel = connection.current?.channel;
    if (channel?.readyState === "open") channel.send(JSON.stringify(event));
  }, []);

  const persistTurns = useCallback(async (turns: VoiceTranscriptPart[][], attempt: number) => {
    let result: string | undefined;
    for (const turn of turns) {
      if (!mounted.current || attempt !== generation.current || projectAtConnection.current !== project.id) return result;
      const request = turn.map((part) => part.delta).join("").trim();
      if (!request) continue;
      const endMs = Math.max(...turn.map((part) => part.endMs));
      // Reserve the transcript boundary before awaiting Core, so a later Live
      // delegation cannot send this same user turn twice.
      delegatedThrough.current = Math.max(delegatedThrough.current, endMs);
      setDelegatedStatus(`Checking: ${request.slice(0, 110)}`);
      result = await onDelegate(request);
      if (attempt === generation.current) lastCoreResult.current = { endMs, text: result?.trim() || "Nebula could not confirm that request. Please check the work status and try again." };
    }
    return result;
  }, [onDelegate, project.id]);

  const flushUnroutedSpeech = useCallback((attempt: number, expectedInputSequence: number) => {
    taskQueue.current = taskQueue.current.then(async () => {
      if (!mounted.current || attempt !== generation.current
        || projectAtConnection.current !== project.id || lastInputSequence.current !== expectedInputSequence) return;
      const turns = selectDelegatedVoiceTurns(transcript.current, delegatedThrough.current, Number.MAX_SAFE_INTEGER);
      if (!turns.length) return;
      try {
        await persistTurns(turns, attempt);
        if (mounted.current && attempt === generation.current) {
          setDelegatedStatus("Nebula saved your speech");
          void refreshWork();
        }
      } catch (caught) {
        logCaughtDiagnostic("interface.voice.unrouted_speech_failed", "Speech without a Live delegation could not be saved to Butler.", caught, "voice-work");
        if (mounted.current && attempt === generation.current) {
          setError("That speech could not be saved to your Butler conversation. Please repeat it.");
          setDelegatedStatus("Speech not saved");
        }
      }
    });
  }, [persistTurns, project.id, refreshWork]);

  const delegate = useCallback(async (id: string, offsetMs: number, attempt: number) => {
    // Live delegation carries an ID and time, not the user's words. Allow late
    // transcript fragments to arrive, then use only new speech for this request.
    const started = Date.now();
    while (Date.now() - started < 1800) {
      const heard = fragments.current.some((part) => part.endMs > delegatedThrough.current && part.startMs <= offsetMs + 700);
      if (heard && Date.now() - lastInputAt.current >= 450) break;
      await sleep(100);
    }
    if (!mounted.current || closing.current || attempt !== generation.current || projectAtConnection.current !== project.id) return;
    const turns = selectDelegatedVoiceTurns(transcript.current, delegatedThrough.current, offsetMs);
    if (!turns.length) {
      const previous = lastCoreResult.current;
      const content = previous && offsetMs >= previous.endMs && offsetMs - previous.endMs < 2_500
        ? previous.text : "I didn't catch enough of that request. Please say it again.";
      sendEvent({ type: "session.commentary.append", event_id: `voice_unclear_${crypto.randomUUID()}`, delegation_id: id, content: spokenResult(content) });
      return;
    }
    try {
      // Earlier direct Live answers and speech captured while another request
      // ran become their own saved Butler turns before this delegated reply.
      const result = await persistTurns(turns, attempt);
      if (!mounted.current || closing.current || attempt !== generation.current || projectAtConnection.current !== project.id) return;
      sendEvent({
        type: "session.commentary.append", event_id: `voice_result_${crypto.randomUUID()}`,
        delegation_id: id,
        content: spokenResult(result?.trim() || "Nebula could not confirm that request. Please check the work status and try again."),
      });
      setDelegatedStatus(result ? "Nebula responded" : "Needs retry");
      void refreshWork();
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.delegation_failed", "The spoken request could not be delegated to Butler.", caught, "voice-work");
      if (!mounted.current || closing.current || attempt !== generation.current) return;
      const message = caught instanceof Error ? caught.message : "The request failed.";
      sendEvent({ type: "session.commentary.append", event_id: `voice_failed_${crypto.randomUUID()}`, delegation_id: id,
        content: `Nebula could not finish the request: ${message.slice(0, 240)}. Please retry or check the work status.` });
      setDelegatedStatus("Request failed. Try again.");
    }
  }, [persistTurns, project.id, refreshWork, sendEvent]);

  const handleEvent = useCallback((event: VoiceEvent) => {
    if (event.type === "session.started") {
      lastActivityAt.current = Date.now();
      setPhase("listening");
      setError("");
    } else if (event.type === "session.closed") {
      const reason = closeReason.current;
      closeReason.current = undefined;
      closing.current = false;
      release();
      if (reason === "exit") onExit();
      else if (reason && (!event.reason || event.reason === "close_requested")) {
        setPhase("paused");
        setPauseMessage(reason === "idle" ? `Paused after ${pauseSeconds.current} seconds of quiet. Project work continues.`
          : reason === "muted" ? "Microphone off. Voice paused; project work continues."
            : reason === "reconnect" ? "Voice paused after connection trouble. Project work continues."
              : "Voice paused while this page was in the background. Project work continues.");
        setError("");
      } else { setPhase("error"); setError("The voice connection ended unexpectedly. Select Resume Voice to start a new session."); }
    } else if (event.type === "session.input_transcript.delta" && typeof event.delta === "string") {
      const start = event.start_ms ?? 0;
      const end = event.end_ms ?? start;
      const part: VoiceTranscriptPart = { speaker: "user", delta: event.delta, startMs: start, endMs: end, sequence: ++transcriptSequence.current };
      fragments.current.push(part);
      fragments.current = fragments.current.slice(-100);
      transcript.current.push(part);
      transcript.current = transcript.current.slice(-160);
      lastInputAt.current = Date.now();
      lastActivityAt.current = lastInputAt.current;
      lastInputSequence.current = part.sequence;
      window.clearTimeout(unroutedTimer.current);
      const attempt = generation.current;
      unroutedTimer.current = window.setTimeout(() => flushUnroutedSpeech(attempt, part.sequence), 1_600);
      const fresh = captionPosition.current?.speaker !== "user" || start - captionPosition.current.endMs > 1_200;
      captionPosition.current = { speaker: "user", endMs: end };
      setCallerCaption((current) => (fresh ? event.delta! : current + event.delta!).slice(-480));
      setPhase("listening");
    } else if (event.type === "session.output_transcript.delta" && typeof event.delta === "string") {
      lastPlaybackAt.current = Date.now();
      lastActivityAt.current = lastPlaybackAt.current;
      const start = event.start_ms ?? 0;
      const end = event.end_ms ?? start;
      transcript.current.push({ speaker: "assistant", delta: event.delta, startMs: start, endMs: end, sequence: ++transcriptSequence.current });
      transcript.current = transcript.current.slice(-160);
      const fresh = captionPosition.current?.speaker !== "assistant" || start - captionPosition.current.endMs > 1_200;
      captionPosition.current = { speaker: "assistant", endMs: end };
      setAssistantCaption((current) => (fresh ? event.delta! : current + event.delta!).slice(-480));
      setPhase("speaking");
      if (!remoteTrack.current) {
        setSpeakerPanelOpen(true);
        setSpeakerMessage("Nebula replied, but no speaker stream arrived. Reconnect voice to try again.");
      }
    } else if (event.type === "session.delegation.created" && event.delegation?.target === "client" && event.delegation.id) {
      const id = event.delegation.id;
      if (delegationIds.current.has(id)) return;
      delegationIds.current.add(id);
      const attempt = generation.current;
      taskQueue.current = taskQueue.current.then(() => delegate(id, event.offset_ms ?? Number.MAX_SAFE_INTEGER, attempt));
    } else if (event.type === "error") {
      setError(event.error?.message || "The voice service reported an error. Try reconnecting if it persists.");
    }
  }, [delegate, flushUnroutedSpeech, onExit, release]);

  const connect = useCallback(async (initial?: Promise<MediaStream>, selectedDeviceId = preferredMicrophone.current) => {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setPhase("error");
      setError("Microphone access needs a trusted HTTPS address on this device, or localhost on the Nebula host. Open Nebula through HTTPS and retry.");
      return;
    }
    const attempt = ++generation.current;
    closing.current = false;
    closeReason.current = undefined;
    mutePending.current = false;
    const previousChannel = connection.current?.channel;
    if (previousChannel?.readyState === "open") {
      previousChannel.send(JSON.stringify({ type: "session.close", event_id: `voice_close_${crypto.randomUUID()}` }));
    }
    window.clearTimeout(unroutedTimer.current);
    release();
    lastActivityAt.current = Date.now();
    lastPlaybackAt.current = 0;
    fragments.current = [];
    transcript.current = [];
    transcriptSequence.current = 0;
    captionPosition.current = undefined;
    lastInputAt.current = 0;
    lastInputSequence.current = 0;
    delegatedThrough.current = 0;
    lastCoreResult.current = undefined;
    delegationIds.current.clear();
    setPhase("connecting");
    setPauseMessage("");
    setError("");
    setPlaybackBlocked(false);
    setSpeakerPanelOpen(false);
    setSpeakerMessage("");
    setSpeakerDevices([]);
    setSpeakerDeviceId("default");
    setMicrophoneMuted(false);
    setCallerCaption("");
    setAssistantCaption("");
    setCallerExpanded(false);
    setAssistantExpanded(false);
    try {
      const microphone = await (initial ?? navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: true,
        noiseSuppression: true,
        ...(selectedDeviceId ? { deviceId: { exact: selectedDeviceId } } : {}),
      } }));
      if (!mounted.current || attempt !== generation.current) { microphone.getTracks().forEach((track) => track.stop()); return; }
      const inputTrack = microphone.getAudioTracks()[0];
      if (!inputTrack) throw new Error("This browser did not return a microphone track. Choose another input and reconnect.");
      setMicrophoneLabel(inputTrack.label || "Active microphone");
      setMicrophoneDeviceId(selectedDeviceId || "default");
      setMicrophoneMessage("");
      inputTrack.addEventListener?.("ended", () => {
        if (attempt !== generation.current || !mounted.current) return;
        setMicrophonePanelOpen(true);
        setMicrophoneMessage("The microphone stopped. Choose another input, then reconnect.");
      });
      inputTrack.addEventListener?.("mute", () => {
        if (attempt !== generation.current || !mounted.current) return;
        setMicrophonePanelOpen(true);
        setMicrophoneMessage("No sound is reaching Nebula from this microphone. Choose another input if it stays silent.");
      });
      startMicrophoneMeter(microphone);
      void refreshMicrophones();
      if (navigator.mediaDevices.enumerateDevices) {
        void navigator.mediaDevices.enumerateDevices().then((devices) => {
          if (!mounted.current || attempt !== generation.current) return;
          setSpeakerDevices(devices.filter((device) => device.kind === "audiooutput" && device.deviceId !== "default")
            .map((device, index) => ({ id: device.deviceId, label: device.label || `Speaker ${index + 1}` })));
        }).catch((caught) => {
          logCaughtDiagnostic("interface.voice.speakers_unavailable", "Speaker devices could not be listed.", caught, "voice-output");
          if (mounted.current && attempt === generation.current) setSpeakerMessage("This browser could not list speakers. Check this device's sound output.");
        });
      }
      const peer = new RTCPeerConnection();
      const channel = peer.createDataChannel("oai-events");
      const current: Connection = { peer, channel, microphone };
      connection.current = current;
      microphone.getAudioTracks().forEach((track) => peer.addTrack(track, microphone));
      peer.addEventListener("track", (event) => {
        if (!audio.current || connection.current !== current || attempt !== generation.current || event.track.kind !== "audio") return;
        remoteTrack.current = event.track;
        const output = new MediaStream([event.track]);
        audio.current.srcObject = output;
        startOutputMeter(output);
        audio.current.muted = false;
        audio.current.volume = 1;
        setSpeakerMessage("");
        void audio.current.play().then(() => setPlaybackBlocked(false)).catch((caught) => {
          logCaughtDiagnostic("interface.voice.playback_blocked", "Voice speaker playback could not start automatically.", caught, "voice-output");
          setPlaybackBlocked(true);
          setSpeakerPanelOpen(true);
          setSpeakerMessage("This browser blocked speaker playback. Select Play audio to hear Nebula.");
        });
      });
      peer.addEventListener("connectionstatechange", () => {
        if (connection.current !== current || attempt !== generation.current || closing.current) return;
        if (peer.connectionState === "failed") { setPhase("error"); setError("The voice connection failed. Select Reconnect to continue."); }
        else if (peer.connectionState === "disconnected") setPhase("reconnecting");
        else if (peer.connectionState === "connected") setPhase((currentPhase) => currentPhase === "reconnecting" ? "listening" : currentPhase);
      });
      channel.addEventListener("message", ({ data }) => {
        if (attempt !== generation.current || connection.current !== current) return;
        try { handleEvent(JSON.parse(String(data)) as VoiceEvent); }
        catch (caught) {
          logCaughtDiagnostic("interface.voice.event_invalid", "A voice event could not be read.", caught, "voice-connection");
          setError("A voice event could not be read. Reconnect if conversation stops.");
        }
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
      pauseSeconds.current = result.auto_pause_seconds ?? 20;
      lastActivityAt.current = Date.now();
      projectAtConnection.current = project.id;
      await peer.setRemoteDescription({ type: "answer", sdp: result.sdp });
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.connection_failed", "The voice connection could not start.", caught, "voice-connection");
      if (attempt !== generation.current || !mounted.current) return;
      release();
      setPhase("error");
      if (caught instanceof DOMException && caught.name === "NotAllowedError") setError("Microphone access was denied. Allow the microphone for this site in your browser, then select Reconnect.");
      else if (caught instanceof DOMException && (caught.name === "NotFoundError" || caught.name === "OverconstrainedError")) {
        setMicrophonePanelOpen(true);
        setError("That microphone is unavailable. Choose another input or reconnect using the system default.");
      }
      else setError(caught instanceof Error ? caught.message : "Voice could not start. Select Reconnect to try again.");
    }
  }, [api, handleEvent, project.id, refreshMicrophones, release, startMicrophoneMeter, startOutputMeter]);

  const closeSession = useCallback((reason: CloseReason) => {
    if (closing.current) return;
    const active = connection.current;
    if (!active) {
      if (reason === "exit") onExit();
      return;
    }
    active.microphone.getAudioTracks().forEach((track) => { track.enabled = false; });
    closing.current = true;
    closeReason.current = reason;
    setPhase("closing");
    const unconfirmed = () => {
      if (connection.current !== active || closeReason.current !== reason) return;
      closeReason.current = undefined;
      closing.current = false;
      release();
      setPhase("error");
      setError("Could not confirm that GPT-Live closed. The device connection was released; check usage before resuming.");
    };
    if (active.channel.readyState !== "open") { unconfirmed(); return; }
    try {
      active.channel.send(JSON.stringify({ type: "session.close", event_id: `voice_close_${crypto.randomUUID()}` }));
      closeTimer.current = window.setTimeout(unconfirmed, 15_000);
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.close_failed", "The voice close request could not be sent.", caught, "voice-connection");
      unconfirmed();
    }
  }, [onExit, release]);

  useEffect(() => {
    mounted.current = true;
    exitButton.current?.focus();
    void connect(initialMicrophone);
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      mounted.current = false;
      ++generation.current;
      window.clearTimeout(unroutedTimer.current);
      document.body.style.overflow = oldOverflow;
      const channel = connection.current?.channel;
      if (channel?.readyState === "open") {
        try { channel.send(JSON.stringify({ type: "session.close", event_id: `voice_close_${crypto.randomUUID()}` })); }
        catch (caught) { logCaughtDiagnostic("interface.voice.unmount_close_failed", "Voice could not request closure during navigation.", caught, "voice-connection"); }
      }
      release();
    };
    // Voice mode is one session per selected project; a changed project is
    // handled by the explicit project effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const checkActivity = async () => {
      const active = connection.current;
      if (!active?.sessionId || closing.current || statsInFlight.current) return;
      if (typeof active.peer.getStats === "function") {
        statsInFlight.current = true;
        try {
          const reports = await Promise.race([active.peer.getStats(), sleep(200).then(() => undefined)]);
          if (reports && connection.current === active) {
            let energy = 0;
            reports.forEach((raw) => {
              const stat = raw as RTCStats & { kind?: string; mediaType?: string; totalAudioEnergy?: number };
              if (stat.type === "inbound-rtp" && (stat.kind === "audio" || stat.mediaType === "audio")) energy += stat.totalAudioEnergy ?? 0;
            });
            const previous = playbackStats.current;
            if (previous?.connection === active && energy - previous.energy > 0.00001) {
              lastPlaybackAt.current = Date.now();
              lastActivityAt.current = lastPlaybackAt.current;
            }
            playbackStats.current = { connection: active, energy };
          }
        } catch (caught) {
          logCaughtDiagnostic("interface.voice.playback_stats_failed", "Voice playback activity could not be measured from WebRTC.", caught, "voice-output");
        } finally { statsInFlight.current = false; }
      }
      if (connection.current !== active || closing.current) return;
      const now = Date.now();
      const playbackActive = now - lastPlaybackAt.current < 1_100;
      if (mutePending.current) {
        if (!playbackActive) closeSession("muted");
      } else if (now - lastActivityAt.current >= pauseSeconds.current * 1_000 && !playbackActive) {
        closeSession("idle");
      }
    };
    const background = () => { if (document.hidden) closeSession("background"); };
    const pageHide = () => closeSession("background");
    const interval = window.setInterval(() => void checkActivity(), 500);
    document.addEventListener("visibilitychange", background);
    window.addEventListener("pagehide", pageHide);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", background);
      window.removeEventListener("pagehide", pageHide);
    };
  }, [closeSession]);

  useEffect(() => {
    void refreshWork();
    const interval = window.setInterval(() => void refreshWork(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshWork]);

  useEffect(() => {
    if (projectAtConnection.current === project.id) return;
    ++generation.current;
    const channel = connection.current?.channel;
    if (channel?.readyState === "open") channel.send(JSON.stringify({ type: "session.close", event_id: `voice_close_${crypto.randomUUID()}` }));
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
      .catch((caught) => {
        if (controller.signal.aborted) return;
        logCaughtDiagnostic("interface.voice.work_detail_failed", "A project work item could not be loaded in Voice.", caught, "voice-work");
        setDetailError("This work item could not be loaded. Refresh its status and try again.");
      });
    return () => controller.abort();
  }, [api, project.id, selected]);

  const exit = () => closeSession("exit");

  const toggleMicrophone = () => {
    if (microphoneMuted) return;
    connection.current?.microphone.getAudioTracks().forEach((track) => { track.enabled = false; });
    mutePending.current = true;
    setMicrophoneMuted(true);
    if (Date.now() - lastPlaybackAt.current >= 1_100) closeSession("muted");
  };

  const chooseMicrophone = (id: string) => {
    preferredMicrophone.current = id === "default" ? undefined : id;
    setMicrophoneDeviceId(id);
    setMicrophoneMessage("Input selected. Resume Voice to use it.");
    closeSession("muted");
  };

  const inspectSpeaker = async () => {
    const element = audio.current;
    const active = connection.current;
    if (!element || !active || !remoteTrack.current || !element.srcObject) {
      setSpeakerMessage("No speaker stream is connected. Select Reconnect voice to try again.");
      return;
    }
    try {
      await element.play();
      setPlaybackBlocked(false);
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.playback_retry_failed", "Voice speaker playback could not be restarted.", caught, "voice-output");
      setPlaybackBlocked(true);
      setSpeakerMessage("This browser blocked speaker playback. Allow sound for this site, then select Play audio.");
      return;
    }
    if (typeof active.peer.getStats !== "function") {
      setSpeakerMessage("Speaker playback started. If silent, check this device's sound output and volume.");
      return;
    }
    try {
      const reports = await active.peer.getStats();
      if (connection.current !== active) return;
      let packets = 0;
      let energy = 0;
      reports.forEach((raw) => {
        const stat = raw as RTCStats & { kind?: string; mediaType?: string; packetsReceived?: number; totalAudioEnergy?: number };
        if (stat.type !== "inbound-rtp" || (stat.kind !== "audio" && stat.mediaType !== "audio")) return;
        packets += stat.packetsReceived ?? 0;
        energy += stat.totalAudioEnergy ?? 0;
      });
      setSpeakerMessage(energy > 0
        ? "Voice audio reached this device. If silent, choose the right speaker or check system volume."
        : packets > 0
          ? "Speaker audio is connected. Ask Nebula to speak again, or choose another output."
          : "No speaker audio packets have arrived yet. Select Reconnect voice and try again.");
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.speaker_stats_failed", "Voice speaker statistics could not be read.", caught, "voice-output");
      setSpeakerMessage("Speaker playback started. If silent, check this device's sound output and volume.");
    }
  };

  const chooseSpeaker = async (id: string) => {
    const element = audio.current as (HTMLAudioElement & { setSinkId?: (deviceId: string) => Promise<void> }) | null;
    if (!element?.setSinkId) return;
    try {
      await element.setSinkId(id);
      setSpeakerDeviceId(id);
      await inspectSpeaker();
    } catch (caught) {
      logCaughtDiagnostic("interface.voice.speaker_selection_failed", "The selected speaker could not be used.", caught, "voice-output");
      setSpeakerMessage("This browser could not use that speaker. Choose another output or check system sound settings.");
    }
  };

  const phaseLabel = phase === "connecting" ? "Connecting" : phase === "speaking" ? "Speaking" : phase === "listening" ? "Listening" : phase === "reconnecting" ? "Reconnecting" : phase === "closing" ? "Ending voice" : phase === "paused" ? "Voice paused" : "Connection needs attention";

  return <div ref={screen} className={styles.screen} role="dialog" aria-modal="true" aria-label="Butler Voice" onKeyDown={(event) => {
    if (event.key === "Escape") { event.preventDefault(); exit(); }
    if (event.key !== "Tab") return;
    const focusables = [...(screen.current?.querySelectorAll<HTMLElement>("button:not(:disabled), select:not(:disabled)") ?? [])];
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }}>
    <audio ref={audio} autoPlay playsInline className={styles.audio} aria-hidden="true" onError={() => {
      setSpeakerPanelOpen(true);
      setSpeakerMessage("Speaker playback failed. Select Play audio or reconnect voice.");
    }} />
    <div className={styles.header}>
      <div className={styles.identity}><strong>Butler <span>Voice</span></strong><small>{project.name}</small></div>
      <button ref={exitButton} className={styles.exit} type="button" title="Exit voice mode" aria-label="Exit voice mode" onClick={exit} disabled={phase === "closing"}><X size={20} aria-hidden="true" /><span>Exit voice mode</span></button>
    </div>
    <div className={styles.layout}>
      <section className={styles.center} aria-label="Voice conversation">
        <div ref={orb} data-testid="voice-orb" className={`${styles.orb} ${phase === "speaking" ? styles.speaking : ""}`} aria-hidden="true"><div /></div>
        <p className={styles.phase} role="status">{microphoneMuted && phase !== "error" && phase !== "paused" ? "Microphone off" : phaseLabel}</p>
        <div className={styles.controls}>
          <button type="button" onClick={toggleMicrophone} disabled={!connection.current || microphoneMuted || phase === "closing"} title={microphoneMuted ? "Microphone off" : "Turn microphone off"} aria-label={microphoneMuted ? "Microphone off" : "Turn microphone off"}><MicrophoneIcon muted={microphoneMuted} /></button>
          <button ref={inputToggle} type="button" onClick={() => { const next = !microphonePanelOpen; setMicrophonePanelOpen(next); if (next) { setSpeakerPanelOpen(false); void refreshMicrophones(); } }} disabled={phase === "closing"} title="Microphone input" aria-label="Microphone input" aria-controls={inputPanelId} aria-expanded={microphonePanelOpen}><MicrophoneIcon size={16} /><span>Input</span>{microphonePanelOpen ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}</button>
          <button ref={speakerToggle} type="button" onClick={() => { const next = !speakerPanelOpen; setSpeakerPanelOpen(next); if (next) { setMicrophonePanelOpen(false); void inspectSpeaker(); } }} disabled={phase === "closing"} title="Speaker output" aria-label="Speaker output" aria-controls={speakerPanelId} aria-expanded={speakerPanelOpen}><SpeakerIcon /><span>Speaker</span>{speakerPanelOpen ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}</button>
          {(phase === "paused" || (phase === "error" && !connection.current)) && <button type="button" onClick={() => void connect()} title="Resume Voice" aria-label="Resume Voice"><RefreshCw size={19} /><span>Resume Voice</span></button>}
          {(phase === "reconnecting" || (phase === "error" && connection.current)) && <button type="button" onClick={() => closeSession("reconnect")} title="Pause Voice" aria-label="Pause Voice"><RefreshCw size={19} /><span>Pause Voice</span></button>}
        </div>
        {phase === "paused" && <p className={styles.delegated} role="status">{pauseMessage}</p>}
        {delegatedStatus && <p className={styles.delegated} role="status">{delegatedStatus}</p>}
        {microphonePanelOpen && <div id={inputPanelId} className={styles.speakerPanel} role="group" aria-label="Microphone input">
          <div className={styles.panelStatus}><p role="status">{microphoneMessage || `Using ${microphoneLabel || "the system microphone"}. The egg should move when you talk.`}</p><button type="button" title="Collapse input settings" aria-label="Collapse input settings" onClick={() => { inputToggle.current?.focus(); setMicrophonePanelOpen(false); }}><ChevronUp size={17} aria-hidden="true" /></button></div>
          <label>Input device
            <select value={microphoneDeviceId} onChange={(event) => chooseMicrophone(event.target.value)}>
              <option value="default">System default</option>
              {microphoneDevices.map((device) => <option key={device.id} value={device.id}>{device.label}</option>)}
            </select>
          </label>
          <div className={styles.speakerActions}>
            <button type="button" onClick={() => void refreshMicrophones()}><RefreshCw size={16} aria-hidden="true" />Refresh inputs</button>
          </div>
        </div>}
        {speakerPanelOpen && <div id={speakerPanelId} className={styles.speakerPanel} role="group" aria-label="Speaker audio">
          <div className={styles.panelStatus}><p role="status">{speakerMessage || "Speaker stream connected. If silent, check the output device and volume."}</p><button type="button" title="Collapse speaker settings" aria-label="Collapse speaker settings" onClick={() => { speakerToggle.current?.focus(); setSpeakerPanelOpen(false); }}><ChevronUp size={17} aria-hidden="true" /></button></div>
          {typeof audio.current?.setSinkId === "function" && speakerDevices.length > 0 && <label>Output device
            <select value={speakerDeviceId} onChange={(event) => void chooseSpeaker(event.target.value)}>
              <option value="default">System default</option>
              {speakerDevices.map((device) => <option key={device.id} value={device.id}>{device.label}</option>)}
            </select>
          </label>}
          <div className={styles.speakerActions}>
            <button type="button" onClick={() => void inspectSpeaker()}><Play size={16} aria-hidden="true" />{playbackBlocked ? "Play audio" : "Check speaker"}</button>
            <button type="button" onClick={() => closeSession("muted")}><RefreshCw size={16} aria-hidden="true" />Pause to reconnect</button>
          </div>
        </div>}
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.captions} aria-label="Voice transcript">
          <div className={styles.captionCard}>
            <div className={styles.captionHead}><small>You</small>{callerCaption.length > 100 && <button type="button" title={callerExpanded ? "Collapse your transcript" : "Show full You transcript"} aria-label={callerExpanded ? "Collapse your transcript" : "Show full You transcript"} aria-expanded={callerExpanded} aria-controls={callerCaptionId} onClick={() => setCallerExpanded((value) => !value)}>{callerExpanded ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}</button>}</div>
            <p id={callerCaptionId} className={callerExpanded ? styles.captionFull : callerCaption.length > 100 ? styles.captionPreview : undefined} aria-live="polite">{callerCaption ? callerExpanded ? callerCaption : captionPreview(callerCaption) : "Speak to ask about this project or direct its work."}</p>
          </div>
          <div className={styles.captionCard}>
            <div className={styles.captionHead}><small>Nebula</small>{assistantCaption.length > 100 && <button type="button" title={assistantExpanded ? "Collapse Nebula transcript" : "Show full Nebula transcript"} aria-label={assistantExpanded ? "Collapse Nebula transcript" : "Show full Nebula transcript"} aria-expanded={assistantExpanded} aria-controls={assistantCaptionId} onClick={() => setAssistantExpanded((value) => !value)}>{assistantExpanded ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}</button>}</div>
            <p id={assistantCaptionId} className={assistantExpanded ? styles.captionFull : assistantCaption.length > 100 ? styles.captionPreview : undefined} aria-live="polite">{assistantCaption ? assistantExpanded ? assistantCaption : captionPreview(assistantCaption) : "Ready for a conversation."}</p>
          </div>
        </div>
      </section>
      <aside className={styles.work} aria-label="Project work">
        <div className={styles.workHeader}><h2>Project work</h2><button type="button" title="Refresh work status" aria-label="Refresh work status" onClick={() => void refreshWork()}><RefreshCw size={17} /></button></div>
        {workError && <p role="alert" className={styles.error}>{workError}</p>}
        {!work && !workError && <p className={styles.empty}>Loading work…</p>}
        {work && !work.chats.length && !work.missions.length && <p className={styles.empty}>No chats or missions yet. Ask Nebula to start one.</p>}
        {Boolean(work?.attention?.length) && <div className={styles.attention} aria-label="Work needing attention">
          <strong>{work!.attention!.length} need{work!.attention!.length === 1 ? "s" : ""} attention</strong>
          {work!.attention!.map((item) => <p key={item.action_id}>
            <span>{item.kind === "approval" ? "Approval" : "Question"} · {item.owner}</span>
            <small>{item.summary}</small>
          </p>)}
          {work!.more_attention && <small>Ask Nebula about more pending requests.</small>}
        </div>}
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
    <footer className={styles.footer}><Radio size={16} aria-hidden="true" /><span>Voice pauses after {pauseSeconds.current} seconds of quiet. Resume starts a new session.</span><Info size={15} aria-hidden="true" /></footer>
  </div>;
}
