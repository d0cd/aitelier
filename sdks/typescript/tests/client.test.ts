/**
 * Verify the TS Aitelier client resolves baseUrl with the right precedence:
 *   explicit option > ~/.config/aitelier/config.toml > default
 * and that NO env var (e.g. AITELIER_BASE_URL) is consulted — that's the
 * principled invariant we're locking in with these tests.
 */

import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Aitelier } from "../src/client.js";

const DEFAULT_BASE = "http://localhost:7777";

/**
 * Drop a temp fake HOME with a config.toml, so discoverBaseUrl()
 * reads from there. Returns the tmp dir for cleanup.
 */
function withFakeHome(toml: string | null): string {
  const home = join(tmpdir(), `aitelier-test-${process.pid}-${Date.now()}`);
  if (toml !== null) {
    mkdirSync(join(home, ".config", "aitelier"), { recursive: true });
    writeFileSync(join(home, ".config", "aitelier", "config.toml"), toml);
  } else {
    mkdirSync(home, { recursive: true });
  }
  return home;
}

let prevHome: string | undefined;
let prevEnvBase: string | undefined;
let tmpHome: string | undefined;

beforeEach(() => {
  prevHome = process.env.HOME;
  prevEnvBase = process.env.AITELIER_BASE_URL;
});

afterEach(() => {
  if (prevHome === undefined) delete process.env.HOME;
  else process.env.HOME = prevHome;
  if (prevEnvBase === undefined) delete process.env.AITELIER_BASE_URL;
  else process.env.AITELIER_BASE_URL = prevEnvBase;
  if (tmpHome) {
    try { rmSync(tmpHome, { recursive: true, force: true }); } catch {}
    tmpHome = undefined;
  }
});

describe("baseUrl resolution", () => {
  it("explicit option wins over config file", () => {
    tmpHome = withFakeHome(`[service]\nhost = "from-config"\nport = 9999\n`);
    process.env.HOME = tmpHome;
    const c = new Aitelier({ baseUrl: "http://explicit:1111" });
    expect((c as unknown as { baseUrl: string }).baseUrl).toBe("http://explicit:1111");
  });

  it("falls back to ~/.config/aitelier/config.toml [service] host+port", () => {
    tmpHome = withFakeHome(`[service]\nhost = "remote-host"\nport = 8080\n`);
    process.env.HOME = tmpHome;
    const c = new Aitelier();
    expect((c as unknown as { baseUrl: string }).baseUrl).toBe("http://remote-host:8080");
  });

  it("default when no config file exists", () => {
    tmpHome = withFakeHome(null);
    process.env.HOME = tmpHome;
    const c = new Aitelier();
    expect((c as unknown as { baseUrl: string }).baseUrl).toBe(DEFAULT_BASE);
  });

  it("ignores AITELIER_BASE_URL env var (principled invariant)", () => {
    tmpHome = withFakeHome(null);
    process.env.HOME = tmpHome;
    process.env.AITELIER_BASE_URL = "http://from-env:6666";
    const c = new Aitelier();
    expect((c as unknown as { baseUrl: string }).baseUrl).toBe(DEFAULT_BASE);
  });

  it("ignores malformed TOML and falls through to default", () => {
    tmpHome = withFakeHome("not = valid = toml [[[");
    process.env.HOME = tmpHome;
    const c = new Aitelier();
    expect((c as unknown as { baseUrl: string }).baseUrl).toBe(DEFAULT_BASE);
  });
});
