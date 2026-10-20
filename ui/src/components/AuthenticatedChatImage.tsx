import { useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { ChatContentBlock } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

export function AuthenticatedChatImage({ api, block, className = "chat-content-image" }: { api: ApiClient; block: ChatContentBlock; className?: string }) {
  const previewArtifactId = typeof block.metadata?.preview_artifact_id === "string"
    ? block.metadata.preview_artifact_id
    : block.artifactId;
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!previewArtifactId) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void api.fetchChatImagePreview(previewArtifactId, controller.signal).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    }).catch((error) => {
      if (!controller.signal.aborted) {
        logCaughtDiagnostic("interface.chat.image_preview_failed", "A chat image preview could not be loaded.", error, "chat-media");
        setFailed(true);
      }
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, previewArtifactId]);
  if (failed) return <p role="status">Image preview unavailable. The original remains retained as an artifact.</p>;
  if (!url) return <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading image…</div>;
  return <a href={url} target="_blank" rel="noreferrer" title="Open image preview"><img className={className} src={url} alt={block.alt ?? "Attached image"} /></a>;
}
