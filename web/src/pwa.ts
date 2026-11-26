export type SwStatus = "unsupported" | "registering" | "ready" | "error";

export async function registerServiceWorker(
  onStatus?: (status: SwStatus) => void
): Promise<ServiceWorkerRegistration | null> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
    onStatus?.("unsupported");
    return null;
  }

  onStatus?.("registering");
  try {
    const registration = await navigator.serviceWorker.register("/sw.js", {
      scope: "/",
    });
    onStatus?.("ready");
    return registration;
  } catch {
    onStatus?.("error");
    return null;
  }
}

export function subscribeNetworkStatus(
  onChange: (online: boolean) => void
): () => void {
  if (typeof window === "undefined") {
    return () => {};
  }

  const handleOnline = () => onChange(true);
  const handleOffline = () => onChange(false);

  window.addEventListener("online", handleOnline);
  window.addEventListener("offline", handleOffline);

  return () => {
    window.removeEventListener("online", handleOnline);
    window.removeEventListener("offline", handleOffline);
  };
}
