import { StrictMode } from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const updater = vi.hoisted(() => ({
  getReleaseInfo: vi.fn(),
  checkForUpdate: vi.fn(),
  installAvailableUpdate: vi.fn(),
  restartApplication: vi.fn(),
}));

vi.mock("../api/updater", () => updater);

import { ReleaseSettingsPanel } from "../components/ReleaseSettingsPanel";
import { UpdateBanner } from "../components/UpdateBanner";
import { ReleaseUpdateProvider } from "./ReleaseUpdateContext";

const directRelease = {
  version: "3.0.0",
  commit: "abc123",
  buildTarget: "x86_64-unknown-linux-gnu",
  builtAt: "2026-07-14T12:00:00Z",
  distribution: "direct",
  updateChannel: "stable",
  updaterEnabled: true,
};

const managedRelease = {
  ...directRelease,
  distribution: "managed",
  updateChannel: undefined,
  updaterEnabled: false,
};

function renderUpdateExperience(strict = false) {
  const content = (
    <ReleaseUpdateProvider>
      <UpdateBanner />
      <ReleaseSettingsPanel />
    </ReleaseUpdateProvider>
  );
  return render(strict ? <StrictMode>{content}</StrictMode> : content);
}

describe("shared release update experience", () => {
  beforeEach(() => {
    updater.getReleaseInfo.mockReset().mockResolvedValue(directRelease);
    updater.checkForUpdate.mockReset().mockResolvedValue({
      currentVersion: "3.0.0",
      version: "3.0.1",
    });
    updater.installAvailableUpdate.mockReset().mockResolvedValue(true);
    updater.restartApplication.mockReset().mockResolvedValue(undefined);
  });

  it("checks once under strict effects and shares dismiss, install, and restart state", async () => {
    const user = userEvent.setup();
    let finishInstall: ((installed: boolean) => void) | undefined;
    updater.installAvailableUpdate.mockReturnValue(new Promise<boolean>((resolve) => {
      finishInstall = resolve;
    }));

    renderUpdateExperience(true);

    expect(await screen.findByRole("button", { name: "Update now" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Install 3.0.1" })).toBeVisible();
    expect(updater.checkForUpdate).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Dismiss update notification" }));
    expect(screen.queryByRole("button", { name: "Update now" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install 3.0.1" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Install 3.0.1" }));
    const installingButtons = await screen.findAllByRole("button", { name: "Installing…" });
    expect(installingButtons).toHaveLength(2);
    expect(installingButtons.every((button) => button.hasAttribute("disabled"))).toBe(true);

    await act(async () => finishInstall?.(true));
    const restartButtons = await screen.findAllByRole("button", { name: "Restart now" });
    expect(restartButtons).toHaveLength(2);
    await user.click(restartButtons[0]);
    expect(updater.restartApplication).toHaveBeenCalledTimes(1);
  });

  it("keeps install failures actionable and retries without losing the available version", async () => {
    const user = userEvent.setup();
    updater.installAvailableUpdate
      .mockRejectedValueOnce(new Error("Update signature verification failed."))
      .mockResolvedValueOnce(true);

    renderUpdateExperience();
    await user.click(await screen.findByRole("button", { name: "Update now" }));

    expect(await screen.findByRole("button", { name: "Retry update" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry 3.0.1" })).toBeVisible();
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent("Update signature verification failed.");

    await user.click(screen.getByRole("button", { name: "Retry update" }));
    expect((await screen.findAllByRole("button", { name: "Restart now" }))).toHaveLength(2);
    expect(updater.installAvailableUpdate).toHaveBeenCalledTimes(2);
  });

  it("keeps automatic check failures out of the global banner and allows a manual retry", async () => {
    const user = userEvent.setup();
    updater.checkForUpdate
      .mockRejectedValueOnce(new Error("Update service is offline."))
      .mockResolvedValueOnce(undefined);

    renderUpdateExperience();

    expect(await screen.findByRole("button", { name: "Check again" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Update service is offline.");
    expect(screen.queryByRole("button", { name: "Dismiss update notification" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Nebula is up to date.")).toBeVisible();
    expect(updater.checkForUpdate).toHaveBeenCalledTimes(2);
  });

  it("does not check or expose self-update controls for managed builds", async () => {
    updater.getReleaseInfo.mockResolvedValue(managedRelease);

    renderUpdateExperience();

    expect(await screen.findByText("Updates are supplied by your package manager.")).toBeVisible();
    expect(updater.checkForUpdate).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Update now" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check for updates" })).not.toBeInTheDocument();
  });
});
