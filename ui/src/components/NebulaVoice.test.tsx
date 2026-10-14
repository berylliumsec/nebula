import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { NebulaVoice } from "./NebulaVoice";

class FakeChannel extends EventTarget {
  readyState = "open";
  sent: Array<Record<string, unknown>> = [];
  send(value: string) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = "closed"; this.dispatchEvent(new Event("close")); }
  emit(value: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) })); }
}

class FakePeer extends EventTarget {
  static latest: FakePeer;
  channel = new FakeChannel();
  iceGatheringState = "complete";
  connectionState = "connected";
  localDescription = { sdp: "v=0\r\nclient-offer" };
  remoteDescription?: RTCSessionDescriptionInit;
  constructor() { super(); FakePeer.latest = this; }
  createDataChannel() { return this.channel; }
  addTrack() { return {}; }
  async createOffer() { return { type: "offer", sdp: "v=0\r\nclient-offer" }; }
  async setLocalDescription() {}
  async setRemoteDescription(value: RTCSessionDescriptionInit) { this.remoteDescription = value; }
  close() {}
}

const microphone = { getTracks: () => [{ stop: vi.fn() }], getAudioTracks: () => [{ enabled: true }] } as unknown as MediaStream;
const roster = {
  project: { id: "project", name: "Project" },
  chats: [{ id: "chat", title: "Surface review", backend: "harness", model: "model", state: "working", url: "/chat" }],
  missions: [{ id: "mission", name: "Audit scope", status: "running", backend: "harness", updated_at: "now", url: "/mission" }],
  attention: [{ kind: "approval", action_id: "approval", owner_kind: "chat", owner_id: "chat", owner: "Code agent", summary: "write_file", url: "/chat" }],
  more_chats: false, more_missions: false,
};

describe("Nebula Voice", () => {
  beforeEach(() => {
    vi.stubGlobal("RTCPeerConnection", FakePeer);
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue(microphone) } });
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("connects once, shows project work, delegates transcript text, and stays open until Exit", async () => {
    const user = userEvent.setup();
    const onDelegate = vi.fn().mockResolvedValue("Started a chat. Core saved it.");
    const onExit = vi.fn();
    const api = {
      startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live_1", sdp: "answer" }),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
      getVoiceWorkDetail: vi.fn().mockResolvedValue({ id: "chat", title: "Surface review", url: "/chat", recent_messages: [{ role: "assistant", content: "Reviewing" }] }),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} initialMicrophone={Promise.resolve(microphone)} onDelegate={onDelegate} onExit={onExit} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledWith("project", "v=0\r\nclient-offer"));
    await act(async () => FakePeer.latest.channel.emit({ type: "session.started" }));
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("Listening");
    expect(screen.getByRole("dialog", { name: "Nebula Voice" })).toBeVisible();
    expect(screen.getByText("Surface review")).toBeVisible();
    expect(screen.getByText("1 needs attention")).toBeVisible();
    expect(screen.getByText("write_file")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Surface review/ }));
    expect(await screen.findByText("Reviewing")).toBeVisible();
    await act(async () => {
      FakePeer.latest.channel.emit({ type: "session.input_transcript.delta", delta: "Start a chat to review scope", start_ms: 1000, end_ms: 1700 });
      FakePeer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 1900, delegation: { id: "delegation_1", target: "client" } });
    });
    await waitFor(() => expect(onDelegate).toHaveBeenCalledWith("Start a chat to review scope"), { timeout: 3000 });
    await waitFor(() => expect(FakePeer.latest.channel.sent).toEqual(expect.arrayContaining([expect.objectContaining({ type: "session.commentary.append", delegation_id: "delegation_1", content: "Started a chat. Core saved it." })])));
    expect(onExit).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Exit voice mode" }));
    expect(FakePeer.latest.channel.sent).toEqual(expect.arrayContaining([expect.objectContaining({ type: "session.close" })]));
    await act(async () => FakePeer.latest.channel.emit({ type: "session.closed" }));
    expect(onExit).toHaveBeenCalledOnce();
  });

  it("keeps the full-screen mode visible when microphone access needs HTTPS", async () => {
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    const api = { getVoiceWorkRoster: vi.fn().mockResolvedValue({ ...roster, chats: [], missions: [] }) } as unknown as ApiClient;
    const onExit = vi.fn();
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} onDelegate={vi.fn()} onExit={onExit} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("trusted HTTPS address");
    expect(screen.getByRole("dialog", { name: "Nebula Voice" })).toBeVisible();
    expect(onExit).not.toHaveBeenCalled();
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it("keeps the upstream session error after intentionally releasing its event channel", async () => {
    const api = {
      startVoiceSession: vi.fn().mockRejectedValue(new Error("GPT-Live rejected this offer. Check access and retry.")),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} onDelegate={vi.fn()} onExit={vi.fn()} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledOnce());
    expect(await screen.findByRole("alert")).toHaveTextContent("GPT-Live rejected this offer");
    expect(screen.getByRole("alert")).not.toHaveTextContent("event channel closed");
  });

  it("keeps speaker recovery visible when a spoken reply has no audio track", async () => {
    const api = {
      startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live_1", sdp: "answer" }),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} initialMicrophone={Promise.resolve(microphone)} onDelegate={vi.fn()} onExit={vi.fn()} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Speaker output" })).toBeVisible();
    await act(async () => FakePeer.latest.channel.emit({ type: "session.output_transcript.delta", delta: "Hello there." }));
    expect(screen.getByText("Hello there.")).toBeVisible();
    expect(screen.getByRole("group", { name: "Speaker audio" })).toHaveTextContent("no speaker stream arrived");
    expect(screen.getByRole("button", { name: "Reconnect voice" })).toBeVisible();
  });

  it("binds the remote audio track and starts playback independently of captions", async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    class FakeRemoteMediaStream {
      constructor(public tracks: MediaStreamTrack[]) {}
      getTracks() { return this.tracks; }
    }
    vi.stubGlobal("MediaStream", FakeRemoteMediaStream);
    const api = {
      startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live_1", sdp: "answer" }),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} initialMicrophone={Promise.resolve(microphone)} onDelegate={vi.fn()} onExit={vi.fn()} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledOnce());
    const remote = { kind: "audio" } as MediaStreamTrack;
    await act(async () => FakePeer.latest.dispatchEvent(Object.assign(new Event("track"), { track: remote })));
    const output = document.querySelector("audio") as HTMLAudioElement;
    expect((output.srcObject as unknown as FakeRemoteMediaStream).getTracks()).toEqual([remote]);
    expect(output.muted).toBe(false);
    expect(output.volume).toBe(1);
    expect(play).toHaveBeenCalledOnce();
  });

  it("shows the active microphone and switches to a selected input", async () => {
    const user = userEvent.setup();
    const currentTrack = { enabled: true, label: "iPhone Microphone", stop: vi.fn() };
    const chosenTrack = { enabled: true, label: "MacBook Pro Microphone", stop: vi.fn() };
    const current = { getTracks: () => [currentTrack], getAudioTracks: () => [currentTrack] } as unknown as MediaStream;
    const chosen = { getTracks: () => [chosenTrack], getAudioTracks: () => [chosenTrack] } as unknown as MediaStream;
    const getUserMedia = vi.fn().mockResolvedValue(chosen);
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      getUserMedia,
      enumerateDevices: vi.fn().mockResolvedValue([
        { kind: "audioinput", deviceId: "iphone", label: "iPhone Microphone" },
        { kind: "audioinput", deviceId: "mac", label: "MacBook Pro Microphone" },
      ]),
    } });
    const api = {
      startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live_1", sdp: "answer" }),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} initialMicrophone={Promise.resolve(current)} onDelegate={vi.fn()} onExit={vi.fn()} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledOnce());
    await user.click(screen.getByRole("button", { name: "Microphone input" }));
    expect(screen.getByRole("group", { name: "Microphone input" })).toHaveTextContent("Using iPhone Microphone");
    await user.selectOptions(screen.getByRole("combobox", { name: "Input device" }), "mac");
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledWith({ audio: {
      echoCancellation: true, noiseSuppression: true, deviceId: { exact: "mac" },
    } }));
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledTimes(2));
    expect(currentTrack.stop).toHaveBeenCalledOnce();
    expect(screen.getByRole("group", { name: "Microphone input" })).toHaveTextContent("Using MacBook Pro Microphone");
  });

  it("moves the orb with microphone energy and settles it when muted", async () => {
    const track = { enabled: true, stop: vi.fn() };
    const liveMicrophone = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
    class FakeAudioContext {
      createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }
      createAnalyser() { return { fftSize: 512, connect: vi.fn(), disconnect: vi.fn(), getByteTimeDomainData: (samples: Uint8Array) => samples.fill(180) }; }
      createGain() { return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() }; }
      destination = {};
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
    }
    vi.stubGlobal("AudioContext", FakeAudioContext);
    let frame: FrameRequestCallback | undefined;
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { frame = callback; return 1; });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    const api = {
      startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live_1", sdp: "answer" }),
      getVoiceWorkRoster: vi.fn().mockResolvedValue(roster),
    } as unknown as ApiClient;
    render(<NebulaVoice api={api} project={{ id: "project", name: "Project" }} initialMicrophone={Promise.resolve(liveMicrophone)} onDelegate={vi.fn()} onExit={vi.fn()} />);
    await waitFor(() => expect(api.startVoiceSession).toHaveBeenCalledOnce());
    await act(async () => { frame?.(0); frame?.(16); });
    const orb = screen.getByTestId("voice-orb");
    const moving = Number(orb.style.getPropertyValue("--voice-scale"));
    expect(moving).toBeGreaterThan(1.02);
    expect(Number.parseFloat(orb.style.getPropertyValue("--voice-rise"))).toBeLessThan(-1);
    await userEvent.click(screen.getByRole("button", { name: "Turn microphone off" }));
    await act(async () => { for (let index = 0; index < 10; index += 1) frame?.(32 + index * 16); });
    expect(Number(orb.style.getPropertyValue("--voice-scale"))).toBeLessThan(moving);
    expect(Number.parseFloat(orb.style.getPropertyValue("--voice-rise"))).toBeGreaterThan(-1);
  });
});
