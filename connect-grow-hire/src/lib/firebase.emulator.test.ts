/** The dev-only Firebase Emulator hook in lib/firebase.ts. */
import { afterEach, describe, expect, it, vi } from "vitest";

const sdk = vi.hoisted(() => ({
  connectAuthEmulator: vi.fn(),
  connectFirestoreEmulator: vi.fn(),
  connectStorageEmulator: vi.fn(),
}));
vi.mock("firebase/app", () => ({ initializeApp: vi.fn(() => ({})) }));
vi.mock("firebase/auth", () => ({ getAuth: vi.fn(() => ({ name: "auth" })), connectAuthEmulator: sdk.connectAuthEmulator }));
vi.mock("firebase/firestore", () => ({ getFirestore: vi.fn(() => ({ name: "db" })), connectFirestoreEmulator: sdk.connectFirestoreEmulator }));
vi.mock("firebase/storage", () => ({ getStorage: vi.fn(() => ({ name: "storage" })), connectStorageEmulator: sdk.connectStorageEmulator }));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  vi.clearAllMocks();
});

describe("Firebase emulator hook", () => {
  it("talks to the real project unless VITE_USE_FIREBASE_EMULATOR=1", async () => {
    vi.stubEnv("VITE_USE_FIREBASE_EMULATOR", "");
    const mod = await import("./firebase");
    expect(mod.usingFirebaseEmulator).toBe(false);
    expect(sdk.connectAuthEmulator).not.toHaveBeenCalled();
    expect(sdk.connectFirestoreEmulator).not.toHaveBeenCalled();
  });

  it("connects Auth, Firestore and Storage to the local emulators when enabled in dev", async () => {
    vi.stubEnv("VITE_USE_FIREBASE_EMULATOR", "1");
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const mod = await import("./firebase");
    expect(mod.usingFirebaseEmulator).toBe(true);
    expect(sdk.connectAuthEmulator).toHaveBeenCalledWith({ name: "auth" }, "http://127.0.0.1:9099", { disableWarnings: true });
    expect(sdk.connectFirestoreEmulator).toHaveBeenCalledWith({ name: "db" }, "127.0.0.1", 8180);
    expect(sdk.connectStorageEmulator).toHaveBeenCalledWith({ name: "storage" }, "127.0.0.1", 9199);
  });

  it("never connects in a production build, even with the flag set", async () => {
    vi.stubEnv("VITE_USE_FIREBASE_EMULATOR", "1");
    vi.stubEnv("DEV", false);
    const mod = await import("./firebase");
    expect(mod.usingFirebaseEmulator).toBe(false);
    expect(sdk.connectFirestoreEmulator).not.toHaveBeenCalled();
  });
});
