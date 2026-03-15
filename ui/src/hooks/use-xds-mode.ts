import { useEffect, useState } from "react";

const API_URL = process.env.NODE_ENV === "production" ? "" : "http://localhost:15000";

type XdsSubscriber = (val: boolean) => void;
const xdsSubscribers: XdsSubscriber[] = [];

let xdsMode = false;
let xdsModeKnown = false;
let xdsModePromise: Promise<boolean> | null = null;

// Helper function to fetch config dump from the server
export async function fetchConfigDump(apiUrl: string = API_URL): Promise<any> {
  const resp = await fetch(`${apiUrl}/config_dump`);
  if (!resp.ok) {
    throw new Error(`Failed to fetch config dump: ${resp.status}`);
  }
  return await resp.json();
}

function isXdsEnabledInConfig(configDump: any): boolean {
  return !!configDump?.config?.xds?.address;
}

export function isXdsMode() {
  return xdsMode;
}

export function setAndBroadcastXds(val: boolean) {
  if (xdsMode !== val) {
    xdsMode = val;
    xdsSubscribers.forEach((cb) => cb(val));
  }
  xdsModeKnown = true;
}

export function subscribeXdsMode(cb: XdsSubscriber) {
  xdsSubscribers.push(cb);
  return () => {
    const idx = xdsSubscribers.indexOf(cb);
    if (idx >= 0) xdsSubscribers.splice(idx, 1);
  };
}

export async function ensureXdsModeLoaded(): Promise<boolean> {
  if (xdsModeKnown) {
    return xdsMode;
  }

  if (xdsModePromise) {
    return xdsModePromise;
  }

  xdsModePromise = (async () => {
    try {
      const dumpJson = await fetchConfigDump(API_URL);
      const enabled = isXdsEnabledInConfig(dumpJson);
      setAndBroadcastXds(enabled);
      return enabled;
    } catch (err) {
      console.error("Failed to determine whether XDS mode is enabled", err);
      // If we can't determine, assume non-XDS mode
      setAndBroadcastXds(false);
      return false;
    } finally {
      xdsModePromise = null;
    }
  })();

  return xdsModePromise;
}

/**
 * React hook that returns whether AgentGateway is running in XDS-managed mode.
 * It subscribes to updates so that components re-render automatically if the
 * mode changes during the session.
 */
export function useXdsMode(): boolean {
  const [xds, setXds] = useState<boolean>(isXdsMode());

  useEffect(() => {
    // Ensure XDS mode is loaded when the hook is first used
    ensureXdsModeLoaded();

    return subscribeXdsMode(setXds);
  }, []);

  return xds;
}
