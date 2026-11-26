import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import {
  AiTokensSection,
  buildMcpConnectionUrls,
  classifyPatStatus,
  createAiTokensApiClient,
  normalizeCreatedPatResponse,
  normalizeOAuthGrantItem,
  normalizePatItem,
  normalizeRequestedScopes,
  renderAiTokensSectionHtml,
  type AiTokensApiClient,
  type CreatedPersonalAccessToken,
  type OAuthGrantItem,
  type PersonalAccessTokenItem,
} from "./aiTokensSection";

describe("AI Tokens & Connected Apps helpers", () => {
  it("normalizes PAT scopes so write always implies read and defaults to read-only", () => {
    expect(normalizeRequestedScopes([])).toEqual(["trailforge:read"]);
    expect(normalizeRequestedScopes(["trailforge:read"])).toEqual([
      "trailforge:read",
    ]);
    expect(normalizeRequestedScopes(["trailforge:write"])).toEqual([
      "trailforge:read",
      "trailforge:write",
    ]);
    expect(
      normalizeRequestedScopes(["trailforge:read", "admin:all", "trailforge:write"])
    ).toEqual(["trailforge:read", "trailforge:write"]);
  });

  it("derives clean MCP public, OAuth, and stdio URLs with safe fallback", () => {
    const urls = buildMcpConnectionUrls("https://trails.example.com/");
    expect(urls.publicMcpUrl).toBe("https://trails.example.com/mcp");
    expect(urls.authMcpUrl).toBe("https://trails.example.com/mcp/auth");
    expect(urls.stdioSnippet).toContain(
      'TRAILFORGE_MCP_URL="https://trails.example.com/mcp"'
    );

    const fallback = buildMcpConnectionUrls("javascript:alert(1)");
    expect(fallback.publicMcpUrl).toBe("https://trails.example.com/mcp");
  });

  it("classifies PAT active, revoked, and expired states cleanly", () => {
    const now = Date.parse("2026-11-27T00:00:00Z");
    expect(
      classifyPatStatus({ revoked_at: null, expires_at: null }, now).status
    ).toBe("active");
    expect(
      classifyPatStatus(
        { revoked_at: "2026-11-26T12:00:00Z", expires_at: null },
        now
      ).status
    ).toBe("revoked");
    expect(
      classifyPatStatus(
        { revoked_at: null, expires_at: "2026-11-20T00:00:00Z" },
        now
      ).status
    ).toBe("expired");
    expect(
      classifyPatStatus(
        { revoked_at: null, expires_at: "2027-01-01T00:00:00Z" },
        now
      ).status
    ).toBe("active");
  });

  it("normalizes PAT and OAuth grant API response payloads across shape variations", () => {
    const pat = normalizePatItem({
      id: "pat-1",
      name: "Claude Code",
      token_prefix: "tf_pat_abc123",
      scope: "trailforge:read trailforge:write",
      created_at: "2026-11-26T10:00:00Z",
    });
    expect(pat).toEqual({
      id: "pat-1",
      name: "Claude Code",
      token_prefix: "tf_pat_abc123",
      scopes: ["trailforge:read", "trailforge:write"],
      expires_at: null,
      last_used_at: null,
      revoked_at: null,
      created_at: "2026-11-26T10:00:00Z",
    });

    const created = normalizeCreatedPatResponse({
      id: "pat-2",
      name: "Cursor",
      token_prefix: "tf_pat_xyz789",
      scopes: ["trailforge:read"],
      token: "tf_pat_xyz789_secret_once",
    });
    expect(created.token).toBe("tf_pat_xyz789_secret_once");
    expect(created.token_prefix).toBe("tf_pat_xyz789");

    const grant = normalizeOAuthGrantItem({
      client_id: "tf_cli_chatgpt",
      client_name: "ChatGPT",
      scope: "trailforge:read trailforge:write",
      active_token_count: 2,
      granted_at: "2026-11-26T09:30:00Z",
    });
    expect(grant?.client_name).toBe("ChatGPT");
    expect(grant?.scopes).toEqual(["trailforge:read", "trailforge:write"]);
    expect(grant?.created_at).toBe("2026-11-26T09:30:00Z");
  });

  it("escapes untrusted token/app names and renders minted copy-once secret banner", () => {
    const html = renderAiTokensSectionHtml({
      loading: false,
      busy: false,
      disabledByServer: false,
      error: null,
      notice: null,
      urls: buildMcpConnectionUrls("https://trails.example.com"),
      newlyMintedToken: {
        id: "pat-new",
        name: "<script>alert(1)</script>",
        token_prefix: "tf_pat_a1b2",
        scopes: ["trailforge:read"],
        token: "tf_pat_a1b2_show_once_token",
      },
      tokens: [
        {
          id: "pat-new",
          name: "<script>alert(1)</script>",
          token_prefix: "tf_pat_a1b2",
          scopes: ["trailforge:read"],
        },
      ],
      grants: [
        {
          client_id: "tf_cli_1",
          client_name: "Claude <Web>",
          scopes: ["trailforge:read", "trailforge:write"],
        },
      ],
    });

    expect(html).not.toContain("<script>alert(1)</script>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).toContain("tf_pat_a1b2_show_once_token");
    expect(html).toContain("never be shown again");
    expect(html).toContain("Claude &lt;Web&gt;");
    expect(html).toContain("https://trails.example.com/mcp/auth");
  });
});

describe("AiTokensSection controller & fetch client", () => {
  it("uses cookie credentials: include with zero localStorage Bearer on same-origin fetch client", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              {
                id: "t1",
                name: "Dev CLI",
                token_prefix: "tf_pat_dev01",
                scopes: ["trailforge:read"],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "t2",
            name: "New PAT",
            token_prefix: "tf_pat_new02",
            scopes: ["trailforge:read", "trailforge:write"],
            token: "tf_pat_new02_full_secret",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            grants: [
              {
                client_id: "tf_cli_ag",
                client_name: "Antigravity",
                scopes: ["trailforge:read"],
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    const api = createAiTokensApiClient(fetchMock as unknown as typeof fetch);
    const listed = await api.listTokens();
    expect(listed).toHaveLength(1);
    expect(listed[0]?.token_prefix).toBe("tf_pat_dev01");

    const created = await api.createToken({
      name: "New PAT",
      scopes: ["trailforge:write"],
      expires_in_days: 90,
    });
    expect(created.token).toBe("tf_pat_new02_full_secret");

    await api.revokeToken("t2");
    const grants = await api.listOAuthGrants();
    expect(grants[0]?.client_name).toBe("Antigravity");
    await api.revokeOAuthGrant("tf_cli_ag");

    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) {
      expect(init.credentials).toBe("include");
      const headers = (init.headers ?? {}) as Record<string, string>;
      expect(headers.Authorization).toBeUndefined();
    }
  });

  it("handles create, copy-once dismiss, revoke PAT, revoke OAuth grant, reset, and 404 disabled state", async () => {
    let tokensStore: PersonalAccessTokenItem[] = [];
    let grantsStore: OAuthGrantItem[] = [
      {
        client_id: "tf_cli_claude",
        client_name: "Claude Desktop",
        scopes: ["trailforge:read", "trailforge:write"],
      },
    ];

    const api: AiTokensApiClient = {
      listTokens: vi.fn(async () => [...tokensStore]),
      createToken: vi.fn(async (input) => {
        const created: CreatedPersonalAccessToken = {
          id: "pat-99",
          name: input.name,
          token_prefix: "tf_pat_9900aa",
          scopes: input.scopes,
          token: "tf_pat_9900aa_once_only_value",
          created_at: "2026-11-27T00:00:00Z",
        };
        tokensStore = [created, ...tokensStore];
        return created;
      }),
      revokeToken: vi.fn(async (id) => {
        tokensStore = tokensStore.map((t) =>
          t.id === id ? { ...t, revoked_at: "2026-11-27T00:05:00Z" } : t
        );
      }),
      listOAuthGrants: vi.fn(async () => [...grantsStore]),
      revokeOAuthGrant: vi.fn(async (clientId) => {
        grantsStore = grantsStore.filter((g) => g.client_id !== clientId);
      }),
    };

    const section = new AiTokensSection({
      origin: "https://trails.example.com",
      api,
    });

    await section.refresh();
    expect(section.getState().grants).toHaveLength(1);
    expect(section.getState().tokens).toHaveLength(0);

    await section.createToken({
      name: "My Laptop",
      scopes: ["trailforge:read"],
      expires_in_days: 90,
    });
    expect(section.getState().newlyMintedToken?.token).toBe(
      "tf_pat_9900aa_once_only_value"
    );
    expect(section.getState().tokens).toHaveLength(1);

    section.dismissMintedToken();
    expect(section.getState().newlyMintedToken).toBeNull();

    await section.revokeToken("pat-99");
    expect(section.getState().tokens[0]?.revoked_at).toBeTruthy();

    await section.revokeOAuthGrant("tf_cli_claude");
    expect(section.getState().grants).toHaveLength(0);

    section.reset();
    expect(section.getState().tokens).toEqual([]);
    expect(section.getState().grants).toEqual([]);
    expect(section.getState().newlyMintedToken).toBeNull();

    // 404 from server when TRAILFORGE_ENABLE_MCP=false shows disabled state cleanly
    const disabledApi: AiTokensApiClient = {
      ...api,
      listTokens: vi.fn(async () => {
        throw new ApiError("Not found", 404);
      }),
    };
    const disabledSection = new AiTokensSection({ api: disabledApi });
    await disabledSection.refresh();
    expect(disabledSection.getState().disabledByServer).toBe(true);
    expect(disabledSection.getState().error).toBeNull();
  });
});
