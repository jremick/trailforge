import type {
  ConditionReportCreateInput,
  SyncQueueItem,
  TripUploadJsonPayload,
  UserPinInput,
} from "../types";
import { getOfflineBackend } from "./packStore";

export function generateClientUuid(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  // Deterministic-shape RFC 4122 v4 fallback
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export async function listSyncQueue(): Promise<SyncQueueItem[]> {
  const items = await getOfflineBackend().getAll<SyncQueueItem>("sync_queue");
  return items.sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export async function getSyncQueueCount(): Promise<number> {
  const items = await listSyncQueue();
  return items.length;
}

export async function enqueueOfflineTrip(
  payload: TripUploadJsonPayload
): Promise<SyncQueueItem> {
  const clientUuid =
    payload.client_uuid ?? payload.client_session_id ?? generateClientUuid();
  const item: SyncQueueItem = {
    id: `trip:${clientUuid}`,
    kind: "trip",
    created_at: new Date().toISOString(),
    label: `Trip: ${payload.name}`,
    payload: {
      ...payload,
      client_uuid: clientUuid,
      client_session_id: payload.client_session_id ?? clientUuid,
    },
    last_error: null,
  };
  await getOfflineBackend().put("sync_queue", item.id, item);
  return item;
}

export async function enqueueOfflineReport(
  payload: Omit<ConditionReportCreateInput, "photo">
): Promise<SyncQueueItem> {
  const clientUuid = payload.client_uuid ?? generateClientUuid();
  const item: SyncQueueItem = {
    id: `report:${clientUuid}`,
    kind: "report",
    created_at: new Date().toISOString(),
    label: `Report: ${payload.report_type.replace(/_/g, " ")}`,
    payload: { ...payload, client_uuid: clientUuid },
    last_error: null,
  };
  await getOfflineBackend().put("sync_queue", item.id, item);
  return item;
}

export async function enqueueOfflinePin(
  payload: UserPinInput,
  localPinId?: string | null
): Promise<SyncQueueItem> {
  const id = `pin:${generateClientUuid()}`;
  const item: SyncQueueItem = {
    id,
    kind: "pin",
    created_at: new Date().toISOString(),
    label: `Pin: ${payload.name}`,
    payload,
    local_pin_id: localPinId ?? null,
    last_error: null,
  };
  await getOfflineBackend().put("sync_queue", item.id, item);
  return item;
}

export async function removeSyncQueueItem(id: string): Promise<void> {
  await getOfflineBackend().delete("sync_queue", id);
}

export interface SyncQueueHandlers {
  syncTrip: (payload: TripUploadJsonPayload) => Promise<void>;
  syncReport: (
    payload: Omit<ConditionReportCreateInput, "photo">
  ) => Promise<void>;
  syncPin: (
    payload: UserPinInput,
    localPinId?: string | null
  ) => Promise<void>;
}

export interface SyncFlushResult {
  synced: number;
  failed: number;
  remaining: number;
  authRequired: boolean;
}

/**
 * Flush queued offline trips, condition reports, and personal pins sequentially.
 * Retains failed items cleanly for retry; marks `authRequired` if HTTP 401 occurs.
 */
export async function flushSyncQueue(
  handlers: SyncQueueHandlers
): Promise<SyncFlushResult> {
  const items = await listSyncQueue();
  let synced = 0;
  let failed = 0;
  let authRequired = false;

  for (const item of items) {
    try {
      if (item.kind === "trip") {
        await handlers.syncTrip(item.payload);
      } else if (item.kind === "report") {
        await handlers.syncReport(item.payload);
      } else if (item.kind === "pin") {
        await handlers.syncPin(item.payload, item.local_pin_id);
      }
      await removeSyncQueueItem(item.id);
      synced += 1;
    } catch (err) {
      failed += 1;
      const status =
        err && typeof err === "object" && "status" in err
          ? Number((err as { status: unknown }).status)
          : null;
      if (status === 401 || status === 403) {
        authRequired = true;
      }
      const message = err instanceof Error ? err.message : "Sync failed";
      await getOfflineBackend().put("sync_queue", item.id, {
        ...item,
        last_error: message,
      });
    }
  }

  const remaining = await getSyncQueueCount();
  return { synced, failed, remaining, authRequired };
}
