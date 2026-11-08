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
          let rows: readonly string[] = ["Starting Agent Beacon…"];
          let width = 0;
          let height = 0;
          let frameWidth = 0;
          let nativeMode = false;
          let screen = beaconScreen();
          let nativeRoot = beaconOverlay(screen);
          let visibleState: BeaconView | undefined;
          let buffer = "";
          let stderr = "";
          let child: ChildProcessWithoutNullStreams;
          const close = () => {
            if (!disposed) done();
          };
          const fail = (error: string) => {
            if (disposed) return;
            ctx.ui.notify(`Agent Beacon: ${error}`, "error");
            close();
          };
          child = spawn("python3", [renderer, "--frames", ...(option === "demo" ? ["--demo"] : ["--omp", session!])], {
            cwd: ctx.cwd,
            stdio: ["pipe", "pipe", "pipe"],
          });
          const send = (command: object) => {
            if (!disposed && child.stdin.writable) child.stdin.write(JSON.stringify(command) + "\n");
          };
          send({ type: "context", model: ctx.model?.id || "unknown", label: ctx.cwd, idle: ctx.isIdle() });
          const useNative = () => {
            if (!nativeMode) {
              nativeMode = true;
              send({ type: "view", native: true });
            }
          };
          child.stdin.on("error", (error) => fail(error.message));
          child.on("error", (error) => fail(error.message));
          child.stderr.setEncoding("utf8");
          child.stderr.on("data", (data: string) => { stderr = (stderr + data).slice(-2000); });
          child.stdout.setEncoding("utf8");
          child.stdout.on("data", (data: string) => {
            if (disposed) return;
            buffer += data;
            let newline: number;
            try {
              while ((newline = buffer.indexOf("\n")) >= 0) {
                const frame = JSON.parse(buffer.slice(0, newline));
                buffer = buffer.slice(newline + 1);
                if (frame.state) {
                  visibleState = frame.state.paused && visibleState ? { ...visibleState, paused: true } : frame.state;
                  screen = beaconScreen(visibleState);
                  nativeRoot = beaconOverlay(screen);
                } else {
                  rows = frame.lines;
                  frameWidth = frame.width;
                }
              }
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
            dispose(): void {
              if (disposed) return;
              disposed = true;
              child.stdin.end();
              child.kill();
            },
          };
        }, { overlay: true });
      } catch (error) {
        ctx.ui.notify(`Agent Beacon: ${error instanceof Error ? error.message : String(error)}`, "error");
      } finally {
        open = false;
      }
    },
  });
}
