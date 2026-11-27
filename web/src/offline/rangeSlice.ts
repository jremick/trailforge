export type ParsedByteRange =
  | { status: 200; start: 0; end: number }
  | { status: 206; start: number; end: number }
  | { status: 416 };

/**
 * Parse an RFC 7233 single-range `Range: bytes=...` header against a known byte length.
 */
export function parseByteRange(
  rangeHeader: string | null | undefined,
  totalLength: number
): ParsedByteRange {
  if (!Number.isFinite(totalLength) || totalLength <= 0) {
    return { status: 416 };
  }
  if (!rangeHeader || !rangeHeader.trim()) {
    return { status: 200, start: 0, end: totalLength - 1 };
  }

  const match = /^bytes=(\d*)-(\d*)$/i.exec(rangeHeader.trim());
  if (!match) {
    return { status: 416 };
  }

  const [, rawStart, rawEnd] = match;
  if (!rawStart && !rawEnd) {
    return { status: 416 };
  }

  let start: number;
  let end: number;

  if (!rawStart && rawEnd) {
    // Suffix byte-range: bytes=-500
    const suffix = Number(rawEnd);
    if (!Number.isInteger(suffix) || suffix <= 0) return { status: 416 };
    start = Math.max(0, totalLength - suffix);
    end = totalLength - 1;
  } else {
    start = Number(rawStart);
    end = rawEnd ? Number(rawEnd) : totalLength - 1;
    if (!Number.isInteger(start) || !Number.isInteger(end)) {
      return { status: 416 };
    }
    if (start >= totalLength || start < 0) {
      return { status: 416 };
    }
    end = Math.min(end, totalLength - 1);
    if (end < start) {
      return { status: 416 };
    }
  }

  return { status: 206, start, end };
}

/**
 * Build a standards-compliant HTTP 206 Partial Content (or 200/416) Response
 * from an in-memory ArrayBuffer / Uint8Array so MapLibre `pmtiles://` renders
 * downloaded regional packs 100% offline.
 */
export function sliceArrayBufferResponse(
  data: ArrayBuffer | Uint8Array,
  rangeHeader: string | null | undefined,
  options?: {
    contentType?: string;
    method?: string;
  }
): Response {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  const total = bytes.byteLength;
  const contentType = options?.contentType ?? "application/octet-stream";
  const isHead = (options?.method ?? "GET").toUpperCase() === "HEAD";

  const parsed = parseByteRange(rangeHeader, total);
  if (parsed.status === 416) {
    return new Response(null, {
      status: 416,
      headers: {
        "Accept-Ranges": "bytes",
        "Content-Range": `bytes */${total}`,
      },
    });
  }

  if (parsed.status === 200) {
    const body = isHead
      ? null
      : bytes.buffer instanceof ArrayBuffer &&
          bytes.byteOffset === 0 &&
          bytes.byteLength === bytes.buffer.byteLength
        ? bytes.buffer
        : bytes.slice();
    return new Response(body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Accept-Ranges": "bytes",
        "Content-Length": String(total),
      },
    });
  }

  const sliceLen = parsed.end - parsed.start + 1;
  const body = isHead
    ? null
    : bytes.slice(parsed.start, parsed.end + 1);

  return new Response(body, {
    status: 206,
    statusText: "Partial Content",
    headers: {
      "Content-Type": contentType,
      "Accept-Ranges": "bytes",
      "Content-Range": `bytes ${parsed.start}-${parsed.end}/${total}`,
      "Content-Length": String(sliceLen),
    },
  });
}

/**
 * Compute lowercase SHA-256 hex digest using Web Crypto (`crypto.subtle`).
 * Returns `null` gracefully if Web Crypto SubtleCrypto is unavailable (e.g. non-secure HTTP context).
 */
export async function computeSha256Hex(
  data: ArrayBuffer | Uint8Array
): Promise<string | null> {
  if (
    typeof globalThis.crypto === "undefined" ||
    !globalThis.crypto.subtle ||
    typeof globalThis.crypto.subtle.digest !== "function"
  ) {
    return null;
  }
  let digestInput: BufferSource;
  if (data instanceof ArrayBuffer) {
    digestInput = data;
  } else if (data.buffer instanceof ArrayBuffer) {
    // Pass the non-shared ArrayBuffer view directly to SubtleCrypto (zero 2x RAM spike on 150-400 MB PMTiles)
    digestInput =
      data.byteOffset === 0 && data.byteLength === data.buffer.byteLength
        ? data.buffer
        : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  } else {
    // SharedArrayBuffer cannot be passed directly to SubtleCrypto.digest
    const copy = new Uint8Array(data.byteLength);
    copy.set(data);
    digestInput = copy;
  }
  const hashBuf = await globalThis.crypto.subtle.digest("SHA-256", digestInput);
  return Array.from(new Uint8Array(hashBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function verifySha256Hex(
  data: ArrayBuffer | Uint8Array,
  expectedHex: string | null | undefined
): Promise<"verified" | "mismatch" | "skipped"> {
  const clean = (expectedHex ?? "").trim().toLowerCase();
  if (!clean) return "skipped";
  const actual = await computeSha256Hex(data);
  if (actual === null) return "skipped";
  return actual === clean ? "verified" : "mismatch";
}
