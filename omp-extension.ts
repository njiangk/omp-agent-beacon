import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { beaconScreen, beaconOverlay, type BeaconView } from "./native-view.ts";

const renderer = fileURLToPath(new URL("./beacon.py", import.meta.url));

export default function beaconExtension(pi: ExtensionAPI): void {
  let open = false;
  pi.registerCommand("beacon", {
    description: "Open live Agent Beacon for this OMP session; /beacon demo previews the animation",
    handler: async (args, ctx) => {
      if (ctx.mode !== "tui" || !ctx.hasUI) {
        ctx.ui.notify("/beacon requires the interactive OMP terminal.", "warning");
        return;
      }
      const option = args.trim();
      if (option && option !== "demo") {
        ctx.ui.notify("Usage: /beacon or /beacon demo", "warning");
        return;
      }
      if (open) return;
      const session = ctx.sessionManager.getSessionFile();
      if (!option && !session) {
        ctx.ui.notify("This session has no saved log. Use /beacon demo, or start OMP with session storage enabled.", "warning");
        return;
      }
      open = true;
      try {
        await ctx.ui.custom<void>((tui, _theme, keys, done) => {
          let disposed = false;
          let exited = false;
          let killTimer: NodeJS.Timeout | undefined;
          let rows: readonly string[] = ["Starting Agent Beacon…"];
          let width = 0;
          let height = 0;
          let frameWidth = 0;
          let nativeMode = false;
          let screen = beaconScreen();
          let nativeRoot = beaconOverlay(screen);
          let visibleState: BeaconView | undefined;
          let advisorExpanded = false;
          const refreshScreen = () => {
            screen = beaconScreen(visibleState, advisorExpanded);
            nativeRoot = beaconOverlay(screen);
          };
          let buffer = "";
          let stderr = "";
          const child: ChildProcessWithoutNullStreams = spawn("python3", [renderer, "--frames", ...(option === "demo" ? ["--demo"] : ["--omp", session!])], {
            cwd: ctx.cwd,
            stdio: ["pipe", "pipe", "pipe"],
          });
          const cleanup = () => {
            if (disposed) return;
            disposed = true;
            buffer = "";
            child.stdin.destroy();
            child.stdout.destroy();
            child.stderr.destroy();
            if (!exited) {
              child.kill();
              killTimer = setTimeout(() => {
                if (!exited) child.kill("SIGKILL");
              }, 1000);
              killTimer.unref();
            }
          };
          const close = () => {
            if (disposed) return;
            cleanup();
            done();
          };
          const fail = (error: string) => {
            if (disposed) return;
            ctx.ui.notify(`Agent Beacon: ${error}`, "error");
            close();
          };
          const send = (command: object) => {
            if (disposed || !child.stdin.writable) return;
            const message = JSON.stringify(command) + "\n";
            if (child.stdin.writableLength + Buffer.byteLength(message) > 65536) {
              fail("renderer is not reading input");
              return;
            }
            try {
              child.stdin.write(message);
            } catch (error) {
              fail(error instanceof Error ? error.message : String(error));
            }
          };
          const useNative = () => {
            if (!nativeMode) {
              nativeMode = true;
              send({ type: "view", native: true });
            }
          };
          child.stdin.on("error", (error) => fail(error.message));
          child.on("error", (error) => fail(error.message));
          child.stdout.on("error", (error) => fail(error.message));
          child.stderr.on("error", (error) => fail(error.message));
          child.on("exit", () => {
            exited = true;
            clearTimeout(killTimer);
          });
          send({ type: "context", model: ctx.model?.id || "unknown", label: ctx.cwd, idle: ctx.isIdle() });
          child.stderr.setEncoding("utf8");
          child.stderr.on("data", (data: string) => { stderr = (stderr + data).slice(-2000); });
          child.stdout.setEncoding("utf8");
          child.stdout.on("data", (data: string) => {
            if (disposed) return;
            buffer += data;
            let newline: number;
            try {
              while ((newline = buffer.indexOf("\n")) >= 0) {
                if (newline > 32 * 1024 * 1024) throw new Error("renderer frame exceeds 32 MiB");
                const frame = JSON.parse(buffer.slice(0, newline));
                buffer = buffer.slice(newline + 1);
                if (frame.state) {
                  if (!frame.state.paused && visibleState && frame.state.advisor.calls !== visibleState.advisor.calls) {
                    advisorExpanded = false;
                  }
                  // Freeze data while allowing navigation within the captured agents.
                  visibleState = frame.state.paused && visibleState
                    ? { ...visibleState, paused: true, page: frame.state.page }
                    : frame.state;
                  refreshScreen();
                } else if (Array.isArray(frame.lines) && frame.lines.every((line: unknown) => typeof line === "string")
                  && Number.isInteger(frame.width) && frame.width > 0 && frame.width <= 1000) {
                  rows = frame.lines;
                  frameWidth = frame.width;
                } else {
                  throw new Error("invalid renderer frame");
                }
              }
              if (buffer.length > 32 * 1024 * 1024) throw new Error("renderer frame exceeds 32 MiB");
              tui.requestRender();
            } catch (error) {
              fail(error instanceof Error ? error.message : String(error));
            }
          });
          child.on("close", (code, signal) => {
            if (disposed) return;
            if (code !== 0) fail(stderr.trim() || `renderer exited (${signal || code})`);
            else close();
          });
          return {
            nativeOverlay: { size: "full", anchor: "center" },
            describeScreen() {
              useNative();
              return screen;
            },
            describe() {
              useNative();
              return nativeRoot;
            },
            handleNativeEvent(event: { type: string; act?: string }): void {
              if (event.type !== "action") return;
              if (event.act === "close") close();
              else if (event.act === "pause") send({ type: "key", key: " " });
              else if (event.act === "next") send({ type: "key", key: "\t" });
              else if (event.act === "advisor-detail") {
                advisorExpanded = !advisorExpanded;
                refreshScreen();
                tui.requestRender();
              }
            },
            render(columns: number): readonly string[] {
              const available = Math.max(1, Math.min(500, process.stdout.rows || 24));
              const nextWidth = Math.max(1, Math.min(1000, columns));
              if (width !== nextWidth || height !== available) {
                width = nextWidth;
                height = available;
                send({ type: "size", width, height });
              }
              // Python produces width-bounded rows; discard stale frames after a resize.
              return frameWidth === width ? rows.slice(0, height) : [];
            },
            handleInput(data: string): void {
              if (data === "q" || data === "\x1b" || data === "\x03" || keys.matches(data, "app.interrupt")) {
                close();
              } else if (data === " " || data === "\t" || data === "r") {
                send({ type: "key", key: data });
              }
            },
            invalidate(): void {},
            dispose: cleanup,
          };
        }, { overlay: true, overlayOptions: { width: "96%", maxHeight: "94%", anchor: "center" } });
      } catch (error) {
        ctx.ui.notify(`Agent Beacon: ${error instanceof Error ? error.message : String(error)}`, "error");
      } finally {
        open = false;
      }
    },
  });
}
